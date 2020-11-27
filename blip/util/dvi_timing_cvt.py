from dataclasses import dataclass

@dataclass
class DVITiming:
    """DVI horizontal/vertical timing values

    Data transmission in DVI is split into two sections: The actual pixel
    data and a blanking period. The blanking period contains a synchronization
    pulse that is preceeded/followed by the front/back porch respectively.
    The polarity of the synchronization pulse signal inside the blanking period
    is used to communicate extended data of the video mode. All timing values
    are expressed as multiples of a pixel clock.

    | pixel clocks | data | sync |
    |--------------|------|------|
    | back_porch   | x    | 0    | 
    | pixels       | data | x    |
    | front_porch  | x    | 0    |
    | sync_pulse   | x    | 1    |
    """

    back_porch: int
    pixels: int
    front_porch: int
    sync_pulse: int
    invert_polarity: bool

@dataclass
class DVIMode:
    """Display mode

    Sync polarities are used to communicate the timing standard:
    | H | V | mode                 |
    |---|---|----------------------|
    | - | - | Non-CVT timing       |
    | - | + | CVT standard CRT     |
    | + | - | CVT reduced blanking |
    | + | + | Non-CVT timing       |
    """

    pixel_clock: float  # < Pixel clock frequency in Hz
    framerate: float    # < Frames per second in Hz
    h: DVITiming        # < Horizontal timing
    v: DVITiming        # < Vertical timing

def get_dvi_mode_cvt_rb(width: int, height: int, framerate: int=60) -> DVIMode:
    """Get timings for a resolution using the CVT-RB standard."""

    # VESA-CVT-v1.2 5.5 Definition of Constants & Variables

    cell_gran_rnd = 8 # Character cell extents (8x8 by default)

    # Reduced blanking constants:
    rb_min_v_blank = 460   # Minimum vertical blank time
    rb_v_fporch = 3        # Vertical front porch time
    min_v_bporch = 6       # Minimum vertical back porch time
    rb_h_blank = 160       # Horizontal blank time
    clock_step = 0.25      # Clock step in MHz
    refresh_multiplier = 1 # Framerate multiplier

    # The vertical blank time for reduced blanking depends on the aspect ratio
    if height*4 == width*3:
        v_sync_rnd = 4 # 4:3
    elif height*16 == width*9:
        v_sync_rnd = 5 # 16:9
    elif height*16 == width*10:
        v_sync_rnd = 6 # 16:10
    elif height*5 == width*4 or height*15 == width*9:
        v_sync_rnd = 7 # Special case 5:4 or 15:9
    else:
        v_sync_rnd = 10 # Non-standard

    h_pixels = width
    v_lines = height
    ip_freq_rqd = framerate

    # VESA-CVT-v1.2 5.2 Computation of Common Parameters

    # 1: Find the refresh rate required (Hz), interlacing is not supported
    v_field_rate_rqd = ip_freq_rqd
    # 2: Round the horizontal resolution to character cells
    h_pixels_rnd = int(h_pixels / cell_gran_rnd) * cell_gran_rnd
    # 3-7: Margins/interlacing are not supported so these are pretty simple..
    left_margin = right_margin = 0
    total_active_pixels = h_pixels_rnd + left_margin + right_margin
    v_lines_rnd = v_lines
    top_margin = bot_margin = 0
    interlace = 0

    # VESA-CVT-v1.2 5.4 Computation of Reduced Blanking Timing Parameters
    # 8: Estimate the Horizontal Period (kHz)
    h_period_est = ((1e6 / v_field_rate_rqd - rb_min_v_blank) /
        (v_lines_rnd + top_margin + bot_margin))
    # 9: Determine the number of lines in the vertical blanking interval
    vbi_lines = int(rb_min_v_blank / h_period_est) + 1
    # 10: Check vertical blanking is sufficient
    rb_min_vbi = rb_v_fporch + v_sync_rnd + min_v_bporch
    act_vbi_lines = max(rb_min_vbi, vbi_lines)
    # 11: Find total number of vertical lines
    total_v_lines = act_vbi_lines + v_lines_rnd + top_margin + bot_margin + interlace
    # 12: Find total number of pixel clocks per line
    total_pixels = rb_h_blank + total_active_pixels
    # 13: Calculate Pixel Clock Frequency to nearest clock_step MHz
    act_pixel_freq = clock_step * int((v_field_rate_rqd * total_v_lines * total_pixels
        / 1e6 * refresh_multiplier) / clock_step)
    # 14: Find actual Horizontal Frequency (kHz)
    act_h_freq = 1000 * act_pixel_freq / total_pixels
    # 15: Find Actual Field Rate (Hz)
    act_field_rate = 1000 * act_h_freq / total_v_lines
    # 16: Find actual Vertical Refresh Rate (Hz)
    act_frame_rate = act_field_rate

    act_v_bporch = act_vbi_lines - rb_v_fporch - v_sync_rnd

    # VESA-CVT-v1.2 3.4.2 Reduced Blanking Timing Version 1
    # 4: Horizontal blank is always 160 pixel clocks
    h_blank = 160
    # 5: Horizontal sync pulse is always 32 pixel clocks and the trailing
    # edge is located in the center of hte horizontal blank
    h_sync = 32
    h_fporch = h_blank // 2 - h_sync
    h_bporch = h_blank // 2

    return DVIMode(
        pixel_clock=act_pixel_freq*1e6,
        framerate=act_field_rate,
        h=DVITiming(
            back_porch=h_bporch,
            pixels=width,
            front_porch=h_fporch,
            sync_pulse=h_sync,
            invert_polarity=False,
        ),
        v=DVITiming(
            back_porch=act_v_bporch,
            pixels=height,
            front_porch=rb_v_fporch,
            sync_pulse=v_sync_rnd,
            invert_polarity=True,
        ),
    )
