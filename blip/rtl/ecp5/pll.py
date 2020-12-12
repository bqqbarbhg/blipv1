from nmigen import *
from nmigen.build import Platform
from nmigen_boards.ulx3s import ULX3S_85F_Platform
from blip.rtl.pll import PllClock
from typing import Union, Iterable
from itertools import product
from collections import namedtuple
from blip import check, Builder

MHz = 1e6

class FloatRange:
    def __init__(self, lo, hi):
        self.lo = lo
        self.hi = hi

    def __contains__(self, val):
        return self.lo <= val <= self.hi

clki_hzs = FloatRange( 10.000*MHz, 400.000*MHz)
clko_hzs = FloatRange(  3.125*MHz, 400.000*MHz)
vco_hzs  = FloatRange(400.000*MHz, 800.000*MHz)
fb_hzs   = FloatRange( 10.000*MHz, 400.000*MHz)

ref_divs = range(1, 128 + 1)
fb_divs  = range(1, 128 + 1)

clko_names = ["CLKOP", "CLKOS", "CLKOS2"]

Config = namedtuple("Config", "error ref_div fb_div clko_divs")


class Ecp5Pll(Elaboratable):

    def __init__(self, clki_freq: Union[int, float], clkos: Iterable[PllClock]):
        self.i_clk = Signal()
        self.i_rst = Signal()

        if not (1 <= len(clkos) <= 3):
            raise ValueError(f"Bad amount of clock outputs: {len(clkos)}")

        self.o_clk = [Signal(1, name=f"o_clk{n}") for n in range(len(clkos))]
        self.o_vco = Signal()
        self.o_locked = Signal()

        self.clki_hz = clki_freq
        self.clkos = list(clkos)

        if clki_freq not in clki_hzs:
            raise ValueError(f"Bad input clock frequency: {clki_freq}")

        best_config = None

        ref_hz = clki_freq
        for ref_div, fb_div in product(ref_divs, fb_divs):
            fb_hz = ref_hz / ref_div
            vco_hz = fb_hz * fb_div
            if fb_hz not in fb_hzs or vco_hz not in vco_hzs:
                continue

            clk_divs = []

            error = 0.0
            for clko in clkos:
                out_div = min(max(round(vco_hz / clko.frequency), 1), 128)
                out_hz = vco_hz / out_div
                out_err = ((out_hz - clko.frequency) / clko.frequency) ** 2
                error += out_err
                if out_err >= clko.tolerance ** 2:
                    break
                clk_divs.append(out_div)
            else:
                config = Config(error, ref_div, fb_div, clk_divs)
                if not best_config or config < best_config:
                    best_config = config
        
        if not best_config:
            raise ValueError("Could not find a PLL configuration")
        self.config = best_config

    def elaborate(self, platform: Platform) -> Module:
        m = Module()

        params = {
            "a_FREQUENCY_PIN_CLKI": str(self.clki_hz / MHz),
            "a_ICP_CURRENT": "12",
            "a_LPF_RESISTOR": "8",
            "a_MFG_ENABLE_FILTEROPAMP": "1",
            "a_MFG_GMCREF_SEL": "2",
            "i_CLKI": self.i_clk,
            "i_RST": self.i_rst,
            "o_LOCK": self.o_locked,
            "p_FEEDBK_PATH": "INT_OS3", # CLKOS3?
            "p_CLKOS3_ENABLE": "ENABLED",
            "p_CLKOS3_DIV": "1",
            "p_CLKI_DIV": str(self.config.ref_div),
            "p_CLKFB_DIV": str(self.config.fb_div),
            "o_CLKOS3": self.o_vco,
        }

        for o_clk, clko, div, name in zip(self.o_clk, self.clkos, self.config.clko_divs, clko_names):
            params[f"p_{name}_ENABLE"] = "ENABLED"
            params[f"p_{name}_DIV"] = str(div)
            params[f"p_{name}_FPHASE"] = "0"
            params[f"p_{name}_CPHASE"] = "0"
            params[f"o_{name}"] = o_clk
            platform.add_clock_constraint(o_clk, clko.frequency)

        m.submodules.ehxpll = ehxpll = Instance("EHXPLLL", **params)

        return m

@check()
def triple_blinky(bld: Builder):
    platform = ULX3S_85F_Platform()

    class Top(Elaboratable):
        def elaborate(self, platform: Platform) -> Module:
            m = Module()

            m.submodules.pll = pll = Ecp5Pll(platform.default_clk_frequency, [
                PllClock(10*MHz), PllClock(20*MHz), PllClock(30*MHz)
            ])

            m.domains.a = ClockDomain("a")
            m.domains.b = ClockDomain("b")
            m.domains.c = ClockDomain("c")

            m.d.comb += [
                pll.i_clk.eq(ClockSignal()),
                ClockSignal("a").eq(pll.o_clk[0]),
                ClockSignal("b").eq(pll.o_clk[1]),
                ClockSignal("c").eq(pll.o_clk[2]),
            ]

            ca = Signal(32)
            cb = Signal(32)
            cc = Signal(32)
            la = Signal()
            lb = Signal()
            lc = Signal()

            m.domain.a += ca.eq(ca + 1)
            m.domain.b += cb.eq(cb + 1)
            m.domain.c += cc.eq(cc + 1)

            with m.If(ca == 10_000_000):
                m.domain.a += ca.eq(0)
                m.domain.a += la.eq(~la)

            with m.If(cb == 20_000_000):
                m.domain.b += cb.eq(0)
                m.domain.b += lb.eq(~lb)

            with m.If(cc == 30_000_000):
                m.domain.c += cc.eq(0)
                m.domain.c += lc.eq(~lc)

            m.d.comb += [
                platform.request("led", 0).eq(la),
                platform.request("led", 1).eq(lb),
                platform.request("led", 2).eq(lc),
            ]

            return m

    platform.build(Top(), build_dir=bld.prefix_path)
