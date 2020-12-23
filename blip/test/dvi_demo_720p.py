from nmigen import *
from nmigen.build import Platform
from nmigen_boards.ulx3s import ULX3S_85F_Platform
from blip import check, Builder
from blip.rtl.dvi.tmds import TMDSEncoder
from blip.rtl.ecp5.pll import Ecp5Pll, PllClock, MHz
from blip.rtl.ecp5.io import Ecp5OutDdr2, Ecp5EdgeClockSync, Ecp5ClockDiv2
from blip.util.dvi_timing import get_dvi_mode_cvt_rb, DVIMode, DVITiming
from nmigen.lib.fifo import AsyncFIFOBuffered

@check(shared=True)
def synth(bld: Builder):
    platform = ULX3S_85F_Platform()
    dvi_mode = get_dvi_mode_cvt_rb(1280, 720)

    class PixelGenerator(Elaboratable):
        def __init__(self):
            self.o_packed = Signal(3*10)

        def elaborate(self, platform: Platform) -> Module:
            m = Module()

            h_data = dvi_mode.h.pixels
            h_front = h_data + dvi_mode.h.front_porch
            h_sync = h_front + dvi_mode.h.sync_pulse
            h_total = h_sync + dvi_mode.h.back_porch

            v_data = dvi_mode.v.pixels
            v_front = v_data + dvi_mode.v.front_porch
            v_sync = v_front + dvi_mode.v.sync_pulse
            v_total = v_sync + dvi_mode.v.back_porch

            h_count = Signal(range(h_total))
            v_count = Signal(range(v_total))
            v_step = Signal()

            f_count = Signal(8)

            m.d.sync += h_count.eq(h_count + 1)
            with m.If(h_count == h_total - 1):
                m.d.sync += h_count.eq(0)
                m.d.comb += v_step.eq(1)

            with m.If(v_step):
                m.d.sync += v_count.eq(v_count + 1)
                with m.If(v_count == v_total - 1):
                    m.d.sync += v_count.eq(0)
                    m.d.sync += f_count.eq(f_count + 1)

            h_de = Signal(1)
            v_de = Signal(1)
            h_sn = Signal(1)
            v_sn = Signal(1)

            m.submodules.tmds_b = tmds_b = TMDSEncoder(True)
            m.submodules.tmds_g = tmds_g = TMDSEncoder(True)
            m.submodules.tmds_r = tmds_r = TMDSEncoder(True)

            m.d.sync += [
                tmds_r.i_data.eq(h_count[1:] + f_count),
                tmds_g.i_data.eq(v_count - f_count[2:]),
                tmds_b.i_data.eq(h_count - f_count),
            ]

            with m.If(h_count < h_data):
                m.d.comb += h_de.eq(1)
            with m.Elif((h_count >= h_front) & (h_count < h_sync)):
                m.d.comb += h_sn.eq(1)

            with m.If(v_count < v_data):
                m.d.comb += v_de.eq(1)
            with m.Elif((v_count >= v_front) & (v_count < v_sync)):
                m.d.comb += v_sn.eq(1)

            de = h_de & v_de
            m.d.sync += [
                tmds_b.i_en_data.eq(de),
                tmds_b.i_hsync.eq(h_sn ^ dvi_mode.h.invert_polarity),
                tmds_b.i_vsync.eq(v_sn ^ dvi_mode.v.invert_polarity),
                tmds_g.i_en_data.eq(de),
                tmds_r.i_en_data.eq(de),
                self.o_packed[0:10].eq(tmds_b.o_char),
                self.o_packed[10:20].eq(tmds_g.o_char),
                self.o_packed[20:30].eq(tmds_r.o_char),
            ]

            return m

    class TmdsShifter(Elaboratable):
        def __init__(self):
            self.i_packed = Signal(3*10)
            self.o_read = Signal()
            self.o_data = Signal(3)
            self.o_clk = Signal()

        def elaborate(self, platform: Platform) -> Module:
            m = Module()

            r_packed = Signal(3*10)

            pairs = [
                (r_packed[0:10], self.o_data[0]),
                (r_packed[10:20], self.o_data[1]),
                (r_packed[20:30], self.o_data[2]),
                (C(0b0000011111, 10), self.o_clk),
            ]

            # (0) 0123 4567 89?? ????
            # (1) 4567 89ab cdef ghij (read; read_hi)
            # (2) 89ab cdef ghij ????
            # (3) cdef ghij ???? ????
            # (4) ghij 0123 4567 89?? (read; read_lo)

            read_cycle = Signal(5, reset=1)
            read_hi = Signal()
            read_lo = Signal()

            m.d.sclk += [
                read_cycle.eq(Cat(read_cycle[1:], read_cycle[:1])),
                read_hi.eq(read_cycle[1]),
                read_lo.eq(read_cycle[4]),
            ]
            with m.If(read_cycle[1] | read_cycle[4]):
                m.d.comb += self.o_read.eq(1)
                m.d.sclk += r_packed.eq(self.i_packed)

            for ix,(i,o) in enumerate(pairs):
                ddr = DomainRenamer("eclk")(Ecp5OutDdr2())
                reg = Signal(16, name=f"reg{ix}")
                m.submodules[f"ddr{ix}"] = ddr

                m.d.sclk += reg.eq(reg[4:])
                with m.If(read_lo):
                    m.d.sclk += reg[6:16].eq(i)
                with m.If(read_hi):
                    m.d.sclk += reg[4:14].eq(i)

                m.d.comb += [
                    ddr.i.eq(Mux(ClockSignal("sclk"), reg[2:4], reg[0:2])),
                    o.eq(ddr.o),
                ]

            return m

    class Top(Elaboratable):
        def elaborate(self, platform: Platform) -> Module:
            m = Module()

            m.submodules.plla = plla = Ecp5Pll(platform.default_clk_frequency, [
                PllClock(100.0*MHz, error_weight=10.0), # 100MHz helper clock for accurate shift clock
                PllClock(dvi_mode.pixel_clock, tolerance=(1e-20, 0.1)), # Pixel clock
            ])

            # Setup two clock domains
            m.submodules.pllb = pllb = Ecp5Pll(plla.config.clko_hzs[0], [
                PllClock(dvi_mode.pixel_clock * 5, tolerance=0.001), # TMDS 2 bits
            ])

            m.submodules.div = div = Ecp5ClockDiv2(pllb.config.clko_hzs[0])

            m.domains.tmds_eclk = ClockDomain("tmds_eclk")
            m.domains.tmds_sclk = ClockDomain("tmds_sclk")
            m.domains.pixel = ClockDomain("pixel")
            m.d.comb += [
                plla.i_clk.eq(ClockSignal()),
                pllb.i_clk.eq(plla.o_clk[0]),
                div.i.eq(pllb.o_clk[0]),
                ClockSignal("pixel").eq(plla.o_clk[1]),
                ClockSignal("tmds_eclk").eq(pllb.o_clk[0]),
                ClockSignal("tmds_sclk").eq(div.o),
            ]

            m.submodules.fifo = fifo = \
                AsyncFIFOBuffered(width=3*10, depth=4, r_domain="tmds_sclk", w_domain="pixel")

            m.submodules.pixel_gen = pixel_gen = \
                DomainRenamer("pixel")(EnableInserter(fifo.w_rdy)(PixelGenerator()))

            m.submodules.tmds_shifter = tmds_shifter = \
                DomainRenamer({ "eclk": "tmds_eclk", "sclk": "tmds_sclk" })(TmdsShifter())

            hdmi = platform.request("hdmi")

            m.d.comb += [
                tmds_shifter.i_packed.eq(fifo.r_data),
                hdmi.d.eq(tmds_shifter.o_data),
                hdmi.clk.eq(tmds_shifter.o_clk),
                fifo.r_en.eq(tmds_shifter.o_read),
                fifo.w_data.eq(pixel_gen.o_packed),
                fifo.w_en.eq(1),
            ]

            return m

    top = Top()
    plan = platform.build(top, do_build=False)
    bld.exec_plan("synth", plan)
