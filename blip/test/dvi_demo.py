from nmigen import *
from nmigen.build import Platform
from nmigen_boards.ulx3s import ULX3S_85F_Platform
from blip import check, Builder
from blip.rtl.dvi.tmds import TMDSEncoder
from blip.rtl.ecp5.pll import Ecp5Pll, PllClock
from blip.rtl.ecp5.io import Ecp5OutDdr2
from blip.util.dvi_timing import get_dvi_mode_cvt_rb, DVIMode, DVITiming
from nmigen.lib.fifo import AsyncFIFOBuffered

@check()
def dvi_demo(bld: Builder):
    platform = ULX3S_85F_Platform()
    dvi_mode = get_dvi_mode_cvt_rb(800, 480)

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

            phase = Signal(range(5))
            read = Signal()
            m.d.sync += phase.eq(Mux(read, 0, phase + 1))
            m.d.comb += read.eq(phase == 4)

            m.d.comb += self.o_read.eq(read)

            pairs = [
                (self.i_packed[0:10], self.o_data[0]),
                (self.i_packed[10:20], self.o_data[1]),
                (self.i_packed[20:30], self.o_data[2]),
                (C(0b0000011111, 10), self.o_clk),
            ]

            for ix,(i,o) in enumerate(pairs):
                ddr = Ecp5OutDdr2()
                reg = Signal(10, name=f"reg{ix}")
                m.submodules[f"ddr{ix}"] = ddr
                with m.If(read):
                    m.d.sync += reg.eq(i)
                m.d.comb += [
                    ddr.i.eq(i.word_select(phase, 2)),
                    o.eq(ddr.o),
                ]

            return m

    class Top(Elaboratable):
        def elaborate(self, platform: Platform) -> Module:
            m = Module()

            # Setup two clock domains
            m.submodules.pll = pll = Ecp5Pll(platform.default_clk_frequency, [
                PllClock(dvi_mode.pixel_clock * 5, error_weight=100.0, tolerance=0.01), # TMDS 2 bits
                PllClock(dvi_mode.pixel_clock, tolerance=(1e-20, 1.0)), # Pixel clock
            ])

            m.domains.tmds_2bit = ClockDomain("tmds_2bit")
            m.domains.pixel = ClockDomain("pixel")
            m.d.comb += [
                pll.i_clk.eq(ClockSignal()),
                ClockSignal("tmds_2bit").eq(pll.o_clk[0]),
                ClockSignal("pixel").eq(pll.o_clk[1]),
            ]

            use_fifo = True

            if use_fifo:
                m.submodules.fifo = fifo = \
                    AsyncFIFOBuffered(width=3*10, depth=4, r_domain="tmds_2bit", w_domain="pixel")

                m.submodules.pixel_gen = pixel_gen = \
                    DomainRenamer("pixel")(EnableInserter(fifo.w_rdy)(PixelGenerator()))
            else:
                m.submodules.pixel_gen = pixel_gen = \
                    DomainRenamer("pixel")(PixelGenerator())

            m.submodules.tmds_shifter = tmds_shifter = \
                DomainRenamer("tmds_2bit")(TmdsShifter())

            hdmi = platform.request("hdmi")

            if use_fifo:
                m.d.comb += [
                    tmds_shifter.i_packed.eq(fifo.r_data),
                    hdmi.d.eq(tmds_shifter.o_data),
                    hdmi.clk.eq(tmds_shifter.o_clk),
                    fifo.r_en.eq(tmds_shifter.o_read),
                    fifo.w_data.eq(pixel_gen.o_packed),
                    fifo.w_en.eq(1),
                ]
            else:
                m.d.pixel += tmds_shifter.i_packed.eq(pixel_gen.o_packed),
                m.d.comb += [
                    hdmi.d.eq(tmds_shifter.o_data),
                    hdmi.clk.eq(tmds_shifter.o_clk),
                ]

            return m

    top = Top()
    plan = platform.build(top, do_build=False)
    bld.exec_plan("synth", plan)

