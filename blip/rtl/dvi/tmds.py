from nmigen import *
from nmigen.asserts import Assert, Assume, Cover, AnyConst
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

    def elaborate(self, platform):
        m = Module()

        use_xnor = Signal()
        xored = Signal(8)
        xnored = Signal(8)
        ctl_char = Signal(10)
        i_pop = Signal(4)

        m.d.comb += [
            i_pop.eq(popcount(self.i_data[1:])),
            use_xnor.eq(i_pop > 3),
        ]

        m.d.comb += xored[0].eq(self.i_data[0])
        for n in range(1, 8):
            m.d.comb += xored[n].eq(self.i_data[n] ^ xored[n - 1])

        for n in range(0, 8):
            if n % 2 == 1:
                m.d.comb += xnored[n].eq(xored[n] ^ use_xnor)
            else:
                m.d.comb += xnored[n].eq(xored[n])

        m.d.comb += [
            self.o_char.eq(Cat(xnored, ~use_xnor, 0))
        ]

        with m.Switch(Cat(self.i_hsync, self.i_vsync)):
            with m.Case(0b00): m.d.comb += ctl_char.eq(0b1101010100)
            with m.Case(0b01): m.d.comb += ctl_char.eq(0b0010101011)
            with m.Case(0b10): m.d.comb += ctl_char.eq(0b0101010100)
            with m.Case(0b11): m.d.comb += ctl_char.eq(0b1010101011)

        with m.If(~self.i_en_data):
            m.d.comb += self.o_char.eq(ctl_char)

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
        
        xored = Signal(8)
        use_xnor = Signal()

        m.d.comb += use_xnor.eq(~self.i_char[8])

        m.d.comb += xored[0].eq(self.i_char[0])
        for n in range(1, 8):
            m.d.comb += xored[n].eq(self.i_char[n] ^ self.i_char[n - 1] ^ use_xnor)

        m.d.comb += [
            self.o_data.eq(xored),
            self.o_en_data.eq(1),
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

    in_data = AnyConst(8)
    en_data = AnyConst(1)
    hsync = AnyConst(1)
    vsync = AnyConst(1)

    m.submodules.enc = enc = TMDSEncoder()
    m.submodules.dec = dec = TMDSDecoder()

    m.d.comb += [
        enc.i_data.eq(in_data),
        enc.i_en_data.eq(en_data),
        enc.i_hsync.eq(hsync),
        enc.i_vsync.eq(vsync),
        dec.i_char.eq(enc.o_char),
    ]

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
        il_text = rtlil.convert(m)
        f.write(il_text)

@check(shared=True)
def bmc(bld: Builder):
    build_formal(bld)
    sby.verify(bld, "bmc.sby", "formal.il",
        sby.Task("sby_bmc", "bmc", depth=40, engines=["smtbmc", "boolector"]),
    )

@check(shared=True)
def cover(bld: Builder):
    build_formal(bld)
    sby.verify(bld, "cover.sby", "formal.il",
        sby.Task("sby_cover", "cover", depth=40, engines=["smtbmc", "boolector"]),
    )
