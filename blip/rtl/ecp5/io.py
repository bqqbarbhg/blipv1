from nmigen import *
from nmigen.build import Platform
from blip import check, Builder
from blip.rtl.ecp5.pll import Ecp5Pll, PllClock, MHz
from nmigen_boards.ulx3s import ULX3S_85F_Platform

class Ecp5OutDdr2(Elaboratable):
    def __init__(self):
        self.i = Signal(2)
        self.o = Signal()

    def elaborate(self, platform):
        m = Module()

        m.submodules.oddrx1f = Instance("ODDRX1F",
            i_SCLK=ClockSignal(),
            i_RST=0,
            i_D0=self.i[0],
            i_D1=self.i[1],
            o_Q=self.o,
        )

        return m

class Ecp5OutDdr4(Elaboratable):
    def __init__(self):
        self.i = Signal(4)
        self.sclk = Signal()
        self.eclk = Signal()
        self.o = Signal()

    def elaborate(self, platform):
        m = Module()

        m.submodules.oddrx2f = Instance("ODDRX2F",
            i_SCLK=self.sclk,
            i_ECLK=self.eclk,
            i_RST=0,
            i_D0=self.i[0],
            i_D1=self.i[1],
            i_D2=self.i[2],
            i_D3=self.i[3],
            o_Q=self.o,
        )

        return m

class Ecp5ClockDiv2(Elaboratable):
    def __init__(self, hz):
        self.i = Signal()
        self.o = Signal()
        self.hz = hz // 2

    def elaborate(self, platform):
        m = Module()

        platform.add_clock_constraint(self.o, self.hz)

        m.submodules.clkdivf = Instance("CLKDIVF",
            i_CLKI=self.i,
            i_RST=0,
            o_CDIVX=self.o,
        )

        return m

class Ecp5EdgeClockSync(Elaboratable):
    def __init__(self, hz):
        self.i = Signal()
        self.o = Signal()
        self.hz = hz

    def elaborate(self, platform):
        m = Module()

        platform.add_clock_constraint(self.o, self.hz)

        m.submodules.eclksyncb = Instance("ECLKSYNCB",
            i_ECLKI=self.i,
            i_STOP=0,
            o_ECLKO=self.o,
        )

        return m

@check()
def mega_blinky(bld: Builder):
    platform = ULX3S_85F_Platform()

    class Top(Elaboratable):
        def elaborate(self, platform: Platform) -> Module:
            m = Module()

            m.submodules.ddr = ddr = Ecp5OutDdr2()
            m.d.comb += [
                ddr.i.eq(0b10),
                platform.request("led", 0).eq(ddr.o),
            ]

            return m

    plan = platform.build(Top(), do_build=False)
    bld.exec_plan("synth", plan)

@check()
def giga_blinky(bld: Builder):
    platform = ULX3S_85F_Platform()

    class Top(Elaboratable):
        def elaborate(self, platform: Platform) -> Module:
            m = Module()

            m.submodules.pll = pll = Ecp5Pll(platform.default_clk_frequency, [
                PllClock(150*MHz),
            ])

            m.submodules.div = div = Ecp5ClockDiv2(pll.config.clko_hzs[0])
            m.submodules.esync = esync = Ecp5EdgeClockSync(div.hz)

            m.submodules.ddr = ddr = Ecp5OutDdr4()

            m.domains.ddr_e = ClockDomain("ddr_e")
            m.domains.ddr_s = ClockDomain("ddr_s")

            m.d.comb += [
                pll.i_clk.eq(ClockSignal()),
                ClockSignal("ddr_e").eq(esync.o),
                ClockSignal("ddr_s").eq(div.o),
            ]

            m.d.comb += [
                esync.i.eq(pll.o_clk[0]),
                div.i.eq(esync.o),
                ddr.eclk.eq(ClockSignal("ddr_e")),
                ddr.sclk.eq(ClockSignal("ddr_s")),
                ddr.i.eq(0b1010),
                platform.request("led", 0).eq(ddr.o),
            ]

            return m

    plan = platform.build(Top(), do_build=False)
    bld.exec_plan("synth", plan)
