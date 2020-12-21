from nmigen import *
from nmigen.build import Platform
from blip import check, Builder
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

@check()
def turbo_blinky(bld: Builder):
    platform = ULX3S_85F_Platform()

    class Top(Elaboratable):
        def elaborate(self, platform: Platform) -> Module:
            m = Module()

            m.d.submodules.ddr = ddr = Ecp5OutDdr2()
            m.d.comb += [
                ddr.i.eq(0b10),
                platform.request("led", 0).eq(ddr.o),
            ]

            return m

    plan = platform.build(Top(), do_build=False)
    bld.exec_plan("synth", plan)
