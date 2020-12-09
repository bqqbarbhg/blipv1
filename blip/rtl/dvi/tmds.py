from nmigen import *
from nmigen.asserts import Assert, Assume, Cover, AnyConst, AnySeq
from nmigen.cli import main_parser, main_runner
from nmigen.back import rtlil
from blip import check, Builder
from blip.task import sby

def popcount(value):
    result = Const(0, range(len(value)))
    for bit in value:
        result = result + bit
    return result

class TMDSEncoder(Elaboratable):
    def __init__(self):
        self.i_data = Signal(8)
        self.i_en_data = Signal()
        self.i_hsync = Signal()
        self.i_vsync = Signal()
        self.o_char = Signal(10)
        self.dc_bias = Signal(4)

        self._xored = Signal(8)
        self._xnored = Signal(8)

    def elaborate(self, platform):
        m = Module()

        # Select between XOR and XNOR encoding based on input popcount
        # to minimize transitions
        i_pop = Signal(4)
        use_xnor = Signal()
        m.d.comb += [
            i_pop.eq(popcount(self.i_data[1:8])),
            use_xnor.eq(i_pop > 3),
        ]

        # Calculate XOR encoding serially with no dependency to `use_xnor`
        xored = self._xored
        m.d.comb += xored[0].eq(self.i_data[0])
        for n in range(1, 8):
            m.d.comb += xored[n].eq(self.i_data[n] ^ xored[n - 1])

        # Transform XOR to XNOR encoding if `use_xnor` is true
        xnored = self._xnored
        for n in range(0, 8):
            if n % 2 == 1:
                m.d.comb += xnored[n].eq(xored[n] ^ use_xnor)
            else:
                m.d.comb += xnored[n].eq(xored[n])

        # Count the DC bias of the data word (-4 to +4)
        x_bias = Signal(4)
        m.d.comb += x_bias.eq(0b1100 + popcount(xnored)),

        # Check if either the data word or current bias is zero,
        # in which case it doesn't matter whether we invert the signal or not
        zero_bias = Signal()
        m.d.comb += zero_bias.eq((x_bias == 0) | (self.dc_bias == 0)),

        # Check if the current and additional bias have the same sign
        same_bias = Signal()
        m.d.comb += same_bias.eq(x_bias[3] == self.dc_bias[3]),

        # If we have zero current bias make sure `use_invert` is the same
        # as `use_xnor` which will encode to either `0b01` or `0b10`. Otherwise
        # make sure we reduce the current bias instead of increasing it.
        use_invert = Signal()
        m.d.comb += use_invert.eq(Mux(zero_bias, use_xnor, same_bias))

        # Count the final DC bias in the output character including
        # potential data inversion and the control bits
        data_bias = Signal(4)
        m.d.comb += data_bias.eq(Mux(use_invert, -x_bias, x_bias) + ~use_xnor + use_invert - 1)

        # Select a fixed control character to use if necessary
        ctl_char = Signal(10)
        with m.Switch(Cat(self.i_hsync, self.i_vsync)):
            with m.Case(0b00): m.d.comb += ctl_char.eq(0b1101010100)
            with m.Case(0b01): m.d.comb += ctl_char.eq(0b0010101011)
            with m.Case(0b10): m.d.comb += ctl_char.eq(0b0101010100)
            with m.Case(0b11): m.d.comb += ctl_char.eq(0b1010101011)

        # Output either a control character or inverted word
        with m.If(self.i_en_data):
            m.d.comb += [
                self.o_char.eq(Cat(Mux(use_invert, ~xnored, xnored), ~use_xnor, use_invert)),
            ]
        with m.Else():
            m.d.comb += [
                self.o_char.eq(ctl_char),
            ]

        # Update DC bias every clock if data is enabled
        with m.If(self.i_en_data):
            m.d.sync += self.dc_bias.eq(self.dc_bias + data_bias)

        return m

class TMDSDecoder(Elaboratable):
    def __init__(self):
        self.i_char = Signal(10)
        self.o_data = Signal(8)
        self.o_en_data = Signal()
        self.o_hsync = Signal()
        self.o_vsync = Signal()

    def elaborate(self, platform):
        m = Module()
        
        inverted = Signal(8)
        xored = Signal(8)
        use_xnor = Signal()
        use_invert = Signal()

        m.d.comb += use_xnor.eq(~self.i_char[8])
        m.d.comb += use_invert.eq(self.i_char[9])

        m.d.comb += inverted.eq(Mux(use_invert, ~self.i_char[:8], self.i_char[:8]))

        m.d.comb += xored[0].eq(inverted[0])
        for n in range(1, 8):
            m.d.comb += xored[n].eq(inverted[n] ^ inverted[n - 1] ^ use_xnor)

        m.d.comb += [
            self.o_en_data.eq(1),
            self.o_data.eq(xored),
        ]

        with m.Switch(self.i_char):
            with m.Case(0b1101010100):
                m.d.comb += [
                    self.o_data.eq(0),
                    self.o_en_data.eq(0),
                    self.o_hsync.eq(0),
                    self.o_vsync.eq(0),
                ]
            with m.Case(0b0010101011):
                m.d.comb += [
                    self.o_data.eq(0),
                    self.o_en_data.eq(0),
                    self.o_hsync.eq(1),
                    self.o_vsync.eq(0),
                ]
            with m.Case(0b0101010100):
                m.d.comb += [
                    self.o_data.eq(0),
                    self.o_en_data.eq(0),
                    self.o_hsync.eq(0),
                    self.o_vsync.eq(1),
                ]
            with m.Case(0b1010101011):
                m.d.comb += [
                    self.o_data.eq(0),
                    self.o_en_data.eq(0),
                    self.o_hsync.eq(1),
                    self.o_vsync.eq(1),
                ]

        return m

def build_formal(bld: Builder):
    if bld.temp_exists("formal.il"):
        return

    m = Module()

    in_data = AnySeq(8)
    en_data = AnySeq(1)
    hsync = AnySeq(1)
    vsync = AnySeq(1)

    enc_char = Signal(10)

    real_chr_bias = Signal(signed(5))
    real_dc_bias = Signal(signed(5))

    m.submodules.enc = enc = TMDSEncoder()
    m.submodules.dec = dec = TMDSDecoder()

    m.d.comb += [
        enc.i_data.eq(in_data),
        enc.i_en_data.eq(en_data),
        enc.i_hsync.eq(hsync),
        enc.i_vsync.eq(vsync),
        dec.i_char.eq(enc.o_char),
        enc_char.eq(enc.o_char),
    ]

    # Check DC bias
    m.d.comb += [
        real_chr_bias.eq(popcount(enc.o_char) - 5),
        Assert(real_dc_bias >= -5),
        Assert(real_dc_bias <= +5),
        Assert(real_dc_bias[:4] == enc.dc_bias),
    ]
    m.d.sync += [
        real_dc_bias.eq(real_dc_bias + real_chr_bias),
    ]

    with m.If(~enc.i_en_data):
        m.d.sync += real_dc_bias.eq(real_dc_bias)

    m.d.comb += Cover(enc.o_char[8] == 0)

    m.d.comb += Assert(dec.o_en_data == enc.i_en_data)
    with m.If(dec.o_en_data):
        m.d.comb += Assert(dec.o_data == enc.i_data)

        # Check that XNOR choice matches reference algorithm
        in_pop = Signal(4)
        use_xnor = Signal()
        m.d.comb += [
            in_pop.eq(popcount(in_data)),
            use_xnor.eq((in_pop > 4) | ((in_pop == 4) & (in_data[0] == 0))),
            Assert(enc.o_char[8] == ~use_xnor),
        ]

    with m.Else():
        m.d.comb += [
            Assert(dec.o_hsync == enc.i_hsync),
            Assert(dec.o_vsync == enc.i_vsync),
        ]

    with bld.temp_open("formal.il") as f:
        il_text = rtlil.convert(m, ports=[enc_char, enc._xored, enc._xnored, real_chr_bias, real_dc_bias])
        f.write(il_text)

@check(shared=True)
def prove(bld: Builder):
    build_formal(bld)
    sby.verify(bld, "prove.sby", "formal.il",
        sby.Task("sby_prove", "prove", depth=3, engines=["smtbmc", "yices"]),
    )

@check(shared=True)
def cover(bld: Builder):
    build_formal(bld)
    sby.verify(bld, "cover.sby", "formal.il",
        sby.Task("sby_cover", "cover", depth=3, engines=["smtbmc", "yices"]),
    )
