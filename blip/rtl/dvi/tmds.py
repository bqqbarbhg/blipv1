from nmigen import *
from nmigen.build import Platform
from nmigen.back import rtlil
from nmigen.asserts import Assert, Assume, Cover, AnyConst, AnySeq, Initial, Past
from nmigen.cli import main_parser, main_runner
from nmigen_boards.ulx3s import ULX3S_85F_Platform
from blip import check, Builder
from blip.task import sby

def popcount(value):
    result = Const(0, range(len(value)))
    for bit in value:
        result = result + bit
    return result

class TMDSEncoder(Elaboratable):
    def __init__(self, pipeline:bool=False):
        self.i_data = Signal(8)
        self.i_en_data = Signal()
        self.i_hsync = Signal()
        self.i_vsync = Signal()
        self.o_char = Signal(10)
        self.dc_bias = Signal(4)

        self.pipeline = pipeline

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
        xored = Signal(8)
        m.d.comb += xored[0].eq(self.i_data[0])
        for n in range(1, 8):
            m.d.comb += xored[n].eq(self.i_data[n] ^ xored[n - 1])

        # Transform XOR to XNOR encoding if `use_xnor` is true
        xnored = Signal(8)
        for n in range(0, 8):
            if n % 2 == 1:
                m.d.comb += xnored[n].eq(xored[n] ^ use_xnor)
            else:
                m.d.comb += xnored[n].eq(xored[n])

        # Count the DC bias of the data word (-4 to +4)
        x_bias = Signal(4)
        m.d.comb += x_bias.eq(0b1100 + popcount(xnored)),

        if self.pipeline:
            xnored_prev = xnored
            use_xnor_prev = use_xnor
            x_bias_prev = x_bias
            xnored = Signal(8, name="xnored_next")
            use_xnor = Signal(1, name="use_xnor_next")
            x_bias = Signal(4, name="x_bias_next")
            en_data = Signal()
            hsync = Signal()
            vsync = Signal()
            m.d.sync += [
                xnored.eq(xnored_prev),
                use_xnor.eq(use_xnor_prev),
                x_bias.eq(x_bias_prev),
                en_data.eq(self.i_en_data),
                hsync.eq(self.i_hsync),
                vsync.eq(self.i_vsync),
            ]
        else:
            en_data = self.i_en_data
            hsync = self.i_hsync
            vsync = self.i_vsync

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
        with m.Switch(Cat(hsync, vsync)):
            with m.Case(0b00): m.d.comb += ctl_char.eq(0b1101010100)
            with m.Case(0b01): m.d.comb += ctl_char.eq(0b0010101011)
            with m.Case(0b10): m.d.comb += ctl_char.eq(0b0101010100)
            with m.Case(0b11): m.d.comb += ctl_char.eq(0b1010101011)

        # Output either a control character or inverted word
        with m.If(en_data):
            m.d.comb += [
                self.o_char.eq(Cat(Mux(use_invert, ~xnored, xnored), ~use_xnor, use_invert)),
            ]
        with m.Else():
            m.d.comb += [
                self.o_char.eq(ctl_char),
            ]

        # Update DC bias every clock if data is enabled
        with m.If(en_data):
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

    m.d.comb += Cover(
        (Past(dec.o_en_data, 4) & (Past(enc.o_char, 4)[8:10] == 0b00)) &
        (Past(dec.o_en_data, 3) & (Past(enc.o_char, 3)[8:10] == 0b01)) &
        (Past(dec.o_en_data, 2) & (Past(enc.o_char, 2)[8:10] == 0b10)) &
        (Past(dec.o_en_data, 1) & (Past(enc.o_char, 1)[8:10] == 0b11)) &
        (dec.o_vsync))

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
        il_text = rtlil.convert(m, ports=[enc_char, real_chr_bias, real_dc_bias])
        f.write(il_text)

def build_formal_pipe(bld: Builder):
    m = Module()

    in_data = AnySeq(8)
    en_data = AnySeq(1)
    hsync = AnySeq(1)
    vsync = AnySeq(1)

    enc_char_comb = Signal(10)
    enc_char_pipe = Signal(10)

    m.submodules.enc_comb = enc_comb = TMDSEncoder(pipeline=False)
    m.submodules.enc_pipe = enc_pipe = TMDSEncoder(pipeline=True)

    m.d.comb += [
        enc_pipe.i_data.eq(in_data),
        enc_pipe.i_en_data.eq(en_data),
        enc_pipe.i_hsync.eq(hsync),
        enc_pipe.i_vsync.eq(vsync),
        enc_char_pipe.eq(enc_pipe.o_char),
    ]

    m.d.sync += [
        enc_comb.i_data.eq(in_data),
        enc_comb.i_en_data.eq(en_data),
        enc_comb.i_hsync.eq(hsync),
        enc_comb.i_vsync.eq(vsync),
    ]
    m.d.comb += [
        enc_char_comb.eq(enc_comb.o_char),
    ]

    with m.If(~Initial()):
        m.d.comb += [
            Assert(enc_char_pipe == enc_char_comb),
            Assert(enc_pipe.dc_bias == enc_comb.dc_bias),
        ]

    with bld.temp_open("formal.il") as f:
        il_text = rtlil.convert(m, ports=[enc_char_comb, enc_char_pipe])
        f.write(il_text)

@check()
def prove(bld: Builder):
    build_formal(bld)
    sby.verify(bld, "prove.sby", "formal.il",
        sby.Task("sby_prove", "prove", depth=3, engines=["smtbmc", "yices"]),
    )

@check()
def cover(bld: Builder):
    build_formal(bld)
    sby.verify(bld, "cover.sby", "formal.il",
        sby.Task("sby_cover", "cover", depth=8, engines=["smtbmc", "yices"]),
    )

@check()
def prove_pipe(bld: Builder):
    build_formal_pipe(bld)
    sby.verify(bld, "prove.sby", "formal.il",
        sby.Task("sby_prove_pipe", "prove", depth=3, engines=["smtbmc", "yices"]),
    )

class SynthTop(Elaboratable):
    def __init__(self, pipeline: bool):
        self.pipeline = pipeline

    def elaborate(self, platform: Platform) -> Module:
        m = Module()

        shift_in = Signal(8)
        shift_out = Signal(11, reset=1)

        m.submodules.enc = enc = TMDSEncoder(pipeline=self.pipeline)

        dummy_in = platform.request("button_fire")
        dummy_out = platform.request("led")

        m.d.sync += [
            shift_in.eq(Cat(dummy_in, shift_in[0:])),
            enc.i_data.eq(shift_in),
            enc.i_en_data.eq(1),
        ]

        m.d.sync += [
            shift_out.eq(shift_out[1:]),
            dummy_out.eq(shift_out[0]),
        ]
        with m.If(shift_out == 1):
            m.d.sync += shift_out.eq(Cat(enc.o_char, 0b1)),

        return m

@check()
def synth(bld: Builder):
    platform = ULX3S_85F_Platform()
    plan = platform.build(SynthTop(pipeline=False), do_build=False)
    bld.exec_plan("synth", plan)

@check()
def synth_pipe(bld: Builder):
    platform = ULX3S_85F_Platform()
    plan = platform.build(SynthTop(pipeline=True), do_build=False)
    bld.exec_plan("synth", plan)

