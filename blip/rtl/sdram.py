from nmigen import *
from nmigen.back import rtlil
from nmigen.asserts import Assert, Assume, Cover, AnySeq, Past
from dataclasses import dataclass
from enum import IntEnum
from blip import check, Builder, use_asserts
from blip.task import sby

@dataclass
class SDRAMMode:
    burst_length: int = 1
    burst_interleaved: bool = False
    cas_latency: int = False

    def encode(self) -> int:
        bursts = { 0: 0b111, 1: 0b000, 2: 0b001, 4: 0b010, 8: 0b011 }
        interleaves = { False: 0b0, True: 0b1 }
        cases = { 2: 0b010, 3: 0b011 }
        r = 0
        r |= bursts[self.burst_length] << 0
        r |= interleaves[self.burst_interleaved] << 3
        r |= cases[self.cas_latency] << 4
        return r

@dataclass
class SDRAMConfig:

    word_bits: int # Number of bits per word
    col_bits: int  # log2 number of columns
    row_bits: int  # log2 number of rows
    bank_bits: int # log2 number of banks

    c_init: int # Clocks to wait for initialization
    c_cas: int  # Clocks from READ to data available
    c_rcd: int  # Clocks from ACTIVE to READ/WRITE
    c_ras: int  # Clocks from READ/WRITE to PRECHARGE
    c_rc: int   # Clocks from REF to anything else
    c_mrd: int  # Clocks from MRS to anything else

    ref_period: int

    ref_max_pending: int = 9

class PendingCounter(Elaboratable):
    def __init__(self, period, max_pending, slack_lo=2, slack_hi=2):
        self.period = period
        self.max_pending = max_pending

        self.bias_lo = slack_lo
        self.bias_hi = slack_hi
        self.max_pending_lo = max_pending + self.bias_lo
        self.max_pending_lohi = self.max_pending_lo + self.bias_hi

        self.i_remove = Signal()
        self.o_any = Signal()
        self.o_full = Signal()

        self.timer = Signal(range(period+1))
        self.pending = Signal(range(self.max_pending_lohi+1), reset=self.bias_lo)
    
    def elaborate(self, platform):
        m = Module()

        add = Signal()
        sub = Signal()

        # Update counter always
        m.d.sync += [
            add.eq(0),
            sub.eq(0),
            self.pending.eq(self.pending + add - sub),
        ]

        # Register subtract late
        m.d.sync += sub.eq(self.i_remove)

        # Add a pending request every `period` cycles
        m.d.sync += self.timer.eq(self.timer + 1)
        with m.If(self.timer == self.period):
            m.d.sync += [
                self.timer.eq(0),
                add.eq(1),
            ]

        m.d.sync += [
            self.o_any.eq(self.pending > self.bias_lo),
            self.o_full.eq(self.pending >= self.max_pending_lo),
        ]

        if use_asserts(platform):
            with m.If(self.pending == 0):
                m.d.comb += Assert(~sub)
            with m.If(self.pending == self.max_pending_lohi):
                m.d.comb += Assert(~add)

        return m

class State(IntEnum):
    INIT_WAIT = 0
    INIT_REF = 1
    INIT_MRS = 2
    IDLE = 3
    ACT = 4
    WAIT = 5

class Cmd(IntEnum):
    # CS RAS CAS WE A10
    DESL  = 0b00000
    NOP   = 0b10000
    BST   = 0b10010
    READ  = 0b10100
    READA = 0b10101
    WRIT  = 0b10100
    WRITA = 0b10101
    ACT   = 0b11000
    PRE   = 0b11010
    PALL  = 0b11011
    REF   = 0b11100
    MRS   = 0b11111

class SDRAMController(Elaboratable):
    def __init__(self, cfg: SDRAMConfig, mode: SDRAMMode):
        self.cfg = cfg
        self.mode = mode

        self.ref_counter = PendingCounter(cfg.ref_period,
            cfg.ref_max_pending, slack_hi=4)

        self.state = Signal(state)

        self.cmd = Signal(Command)

        self.max_delay = max(cfg.c_cas, cfg.c_rc, cfg.c_rcd, cfg.c_mrd)
        self.wait = Signal(range(self.max_delay), reset_less=True)
        self.wait_state = Signal(State, reset_less=True)

        self.rw_timer = Signal(range(max(mode.burst_length, mode.cas_latency)))

        init_cycles = (cfg.c_init // self.max_delay) + 1
        self.init_ctr = Signal(range(init_cycles + 1), reset=init_cycles)
        self.init_refs = Signal()

        addr_bits = cfg.col_bits + cfg.row_bits + cfg.bank_bits

        self.r_addr = Signal(addr_bits)
        self.r_write = Signal()

        self.col_slice = slice(0, cfg.col_bits)
        self.row_slice = slice(self.col_slice.stop, self.col_slice.stop+cfg.row_bits)
        self.bank_slice = slice(self.row_slice.stop, self.row_slice.stop+cfg.bank_bits)

        self.r_grant = Signal()

        self.cmd = Signal(Cmd, reset=Cmd.NOP)
        self.addr = Signal(addr_bits)
        self.bank = Signal(cfg.bank_bits)

        self.i_req = Signal()
        self.i_write = Signal()
        self.i_addr = Signal(addr_bits)
        self.i_wr_data = Signal(cfg.word_bits)
        self.i_rd_data = Signal(cfg.word_bits)

        self.o_grant = Signal()
        self.o_rd_data = Signal(cfg.word_bits)

        self.o_cs = Signal()
        self.o_ras = Signal()
        self.o_cas = Signal()
        self.o_we = Signal()
        self.o_a = Signal(12)
        self.o_ba = Signal(2)
        self.o_wr_data = Signal(cfg.word_bits)
        self.o_data_en = Signal()
    
    def do(self, m, cmd, next, wait, bank=0, addr=0):
        m.d.comb += [
            self.cmd.eq(cmd),
            self.addr.eq(addr),
            self.bank.eq(bank),
        ]
        if wait > 0:
            m.d.sync += [
                self.state.eq(State.WAIT),
                self.wait.eq(wait - 1),
                self.wait_state.eq(next),
            ]
        else:
            m.d.sync += [
                self.state.eq(next),
            ]

    def elaborate(self, platform):
        cfg, mode = self.cfg, self.mode
        m = Module()

        m.d.sync += self.r_grant.eq(0)
        m.d.comb += self.o_grant.eq(self.r_grant)

        m.d.comb += [
            self.o_cs.eq(self.cmd[0]),
            self.o_ras.eq(self.cmd[1]),
            self.o_cas.eq(self.cmd[2]),
            self.o_we.eq(self.cmd[3]),
            self.o_a.eq(self.addr),
            self.o_ba.eq(self.bank),
            self.o_wr_data.eq(self.i_wr_data),
            self.o_rd_data.eq(self.i_rd_data),
        ]

        # Override a[10]
        m.d.comb += self.o_a[10].eq(self.addr[10] | self.cmd[4])

        with m.If(self.rw_timer != 0):
            m.d.sync += self.rw_timer.eq(self.rw_timer - 1)
            with m.If(self.r_write):
                m.d.comb += self.o_data_en.eq(1)
            with m.Elif(self.rw_timer[:2] == 1):
                m.d.comb += self.r_grant.eq(1)

        with m.Switch(self.state):
            with m.Case(State.INIT_WAIT):
                m.d.sync += self.init_ctr.eq(self.init_ctr - 1)
                with m.If(self.init_ctr == 0):
                    self.do(m, Cmd.PALL, State.INIT_REF, self.max_delay)
                with m.Else():
                    self.do(m, Cmd.NOP, State.INIT_WAIT, self.max_delay)
            with m.Case(State.INIT_REF):
                self.do(m, Cmd.REF, Mux(self.init_refs, State.INIT_MRS, State.INIT_REF), cfg.c_rc)
            with m.Case(State.INIT_MRS):
                self.do(m, Cmd.MRS, State.IDLE, cfg.c_mrd, addr=self.mode.encode())
            with m.Case(State.IDLE):
                with m.If(self.ref_counter.o_all):
                    self.do(m, Cmd.REF, State.IDLE, cfg.c_rc)
                with m.If(self.ref_counter.i_req):
                    m.d.sync += [
                        self.r_addr.eq(self.i_addr),
                        self.r_write.eq(self.i_write),
                    ]
                    self.do(m, Cmd.ACT, State.ACT, cfg.c_rcd,
                        addr=self.i_addr[self.row_slice],
                        bank=self.i_addr[self.bank_slice])
                with m.Elif(self.ref_counter.o_any):
                    self.do(m, Cmd.REF, State.IDLE, cfg.c_rc)
            with m.Case(State.ACT):
                with m.If(self.r_write):
                    m.d.comb += self.o_grant.eq(1)
                    m.d.comb += self.o_data_en.eq(1)
                    m.d.sync += self.rw_timer.eq(mode.burst_length - 1)
                    self.do(m, Cmd.WRITA, State.IDLE,
                        wait=max(cfg.c_ras - cfg.c_rcd, mode.burst_length),
                        addr=self.r_addr[self.col_slice],
                        bank=self.r_addr[self.bank_slice])
                with m.Else():
                    m.d.sync += self.rw_timer.eq(mode.cas_latency - 1)
                    self.do(m, Cmd.READA, State.IDLE,
                        wait=max(cfg.c_ras - cfg.c_rcd, mode.cas_latency + mode.burst_length),
                        addr=self.r_addr[self.col_slice],
                        bank=self.r_addr[self.bank_slice])
            with m.Case(State.WAIT):
                m.d.sync += self.wait.eq(self.wait - 1)
                with m.If(self.wait == 0):
                    m.d.sync += self.state.eq(self.wait_state)

        m.submodules.ref_counter = self.ref_counter

        return m

class SDRAMSimulator(Elaboratable):
    def __init__(self, cfg: SDRAMConfig, mode: SDRAMMode):
        self.cfg = cfg
        self.mode = mode

        self.cmd = Signal(Cmd)
        self.state = Signal(State)

        self.init_ref_count = Signal()

        addr_bits = cfg.col_bits + cfg.row_bits + cfg.bank_bits
        self.mem = Memory(width=cfg.word_bits, depth=addr_bits)

        self.r_col = Signal(cfg.col_bits)
        self.r_row = Signal(cfg.row_bits)
        self.r_bank = Signal(cfg.bank_bits)

        self.i_cs = Signal()
        self.i_ras = Signal()
        self.i_cas = Signal()
        self.i_we = Signal()
        self.i_a = Signal(12)
        self.i_ba = Signal(2)
        self.i_data_en = Signal()
        self.i_wr_data = Signal(cfg.word_bits)

        self.o_rd_data = Signal(cfg.word_bits)

    def do(self, m, next, wait, bank=0, addr=0):
        m.d.comb += [
            self.cmd.eq(cmd),
            self.addr.eq(addr),
            self.bank.eq(bank),
        ]
        if wait > 0:
            m.d.sync += [
                self.state.eq(State.WAIT),
                self.wait.eq(wait - 1),
                self.wait_state.eq(next),
            ]
        else:
            m.d.sync += [
                self.state.eq(next),
            ]


    def elaborate(self, platform):
        mode, cfg = self.mode, self.cfg

        m = Module()

        m.submodules.wr_port = wr_port = self.mem.write_port()
        m.submodules.rd_port = rd_port = self.mem.read_port()

        with m.Switch(self.state):
            with m.Case(State.INIT_WAIT):
                self.do(m, State.INIT_REF, cfg.c_init)
            with m.Case(State.INIT_REF):
                with m.If(self.cmd == Cmd.REF):
                    with m.If(self.init_ref_count == 2 - 1):
                        self.do(m, State.INIT_MRS, cfg.c_ref)
                    with m.Else():
                        self.do(m, State.INIT_REF, cfg.c_ref)
                        m.d.sync += self.init_ref_count.eq(self.init_ref_count + 1)
                with m.Else():
                    if use_asserts(platform):
                        m.d.comb += Assert(self.cmd == Cmd.NOP)
            with m.Case(State.INIT_MRS):
                with m.If(self.cmd == Cmd.MRS):
                    self.do(m, State.IDLE, cfg.c_mrd)
                    if use_asserts(platform):
                        m.d.comb += Assert(self.i_a == mode.encode())
                with m.Else():
                    if use_asserts(platform):
                        m.d.comb += Assert(self.cmd == Cmd.NOP)
            with m.Case(State.IDLE):
                with m.If(self.cmd == Cmd.REF):
                    self.do(m, State.IDLE, cfg.c_ref)
                with m.Elif(self.cmd == Cmd.ACT):
                    self.do(m, State.ACT, cfg.c_rcd)
                    m.d.sync += [
                        self.r_row.eq(self.i_a),
                        self.r_bank.eq(self.i_ba),
                    ]
                with m.Else():
                    if use_asserts(platform):
                        m.d.comb += Assert(self.cmd == Cmd.NOP)
            with m.Case(State.ACT):
                with m.If(self.cmd == Cmd.WRITA):
                    self.do(m, State.IDLE,
                        wait=max(cfg.c_ras - cfg.c_rcd, mode.burst_length))
                    m.d.comb += [
                        wr_port.addr.eq(Cat(self.i_a[:cfg.col_bits], self.r_row, self.r_bank)),
                        wr_port.data.eq(self.i_wr_data),
                        wr_port.en.eq(1),
                    ]
                    m.d.sync += self.r_col.eq(self.i_a)
                with m.If(self.cmd == Cmd.READA):
                    self.do(m, State.IDLE,
                        wait=max(cfg.c_ras - cfg.c_rcd, mode.cas_latency + mode.burst_length))
                    m.d.sync += self.r_col.eq(self.i_a)
                with m.Else():
                    if use_asserts(platform):
                        m.d.comb += Assert(self.cmd == Cmd.NOP)
            with m.Case(State.WAIT):
                m.d.sync += self.wait.eq(self.wait - 1)
                with m.If(self.wait == 0):
                    m.d.sync += self.state.eq(self.wait_state)
                if use_asserts(platform):
                    m.d.comb += Assert(self.cmd == Cmd.NOP)

        return m

@check()
def bmc_pending_counter(bld: Builder):
    m = Module()

    m.submodules.pc = pc = PendingCounter(3, 5)

    m.d.comb += pc.i_remove.eq(AnySeq(1))
    m.d.comb += Assume(~(pc.i_remove & ~pc.o_any))
    m.d.comb += Assume(~(~pc.i_remove & pc.o_full))

    with bld.temp_open("formal.il") as f:
        il_text = rtlil.convert(m, ports=[pc.pending, pc.timer])
        f.write(il_text)

    sby.verify(bld, "formal.sby", "formal.il",
        sby.Task("sby", "bmc", depth=40, engines=["smtbmc", "yices"]),
    )

@check()
def cover_pending_counter(bld: Builder):
    m = Module()

    m.submodules.pc = pc = PendingCounter(3, 5)

    was_full = Signal()
    was_emptied = Signal()

    m.d.comb += pc.i_remove.eq(AnySeq(1))
    m.d.comb += Assume(~(pc.i_remove & ~pc.o_any))
    m.d.comb += Assume(~(~pc.i_remove & pc.o_full))

    with m.If(pc.o_full):
        m.d.sync += was_full.eq(1)
    with m.If(~pc.o_any & was_full):
        m.d.sync += was_emptied.eq(1)
    m.d.comb += Cover(was_emptied)

    with bld.temp_open("formal.il") as f:
        il_text = rtlil.convert(m, ports=[pc.pending, pc.timer])
        f.write(il_text)

    sby.verify(bld, "formal.sby", "formal.il",
        sby.Task("sby", "cover", depth=40, engines=["smtbmc", "yices"]),
    )
