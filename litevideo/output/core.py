from litex.gen import *

from litex.soc.interconnect import stream
from litex.soc.interconnect.csr import AutoCSR
from litex.soc.interconnect import dma_lasmi

from litevideo.csc.ycbcr2rgb import YCbCr2RGB
from litevideo.csc.ycbcr422to444 import YCbCr422to444

from litevideo.spi import IntSequence, SingleGenerator, MODE_CONTINUOUS

from litevideo.output.common import *
from litevideo.output.hdmi.s6 import S6HDMIOutClocking, S6HDMIOutPHY


class FrameInitiator(SingleGenerator):
    """Frame initiator

    Generates a stream of tokens with associated
    H/V parameters.

    This modules controlled from CSR registers generates one token
    to be generated and provides vertical and horizontal parameters.
    Once started, the module generates frames tokens continously and
    can be stopped from CSR.
    """
    def __init__(self, bus_aw, pack_factor, ndmas=1):
        h_alignment_bits = log2_int(pack_factor)
        bus_alignment_bits = h_alignment_bits + log2_int(bpp//8)
        layout = [
            ("hres", hbits, 0, h_alignment_bits),
            ("hsync_start", hbits, 0, h_alignment_bits),
            ("hsync_end", hbits, 0, h_alignment_bits),
            ("hscan", hbits, 0, h_alignment_bits),

            ("vres", vbits),
            ("vsync_start", vbits),
            ("vsync_end", vbits),
            ("vscan", vbits),

            ("length", bus_aw + bus_alignment_bits, 0, bus_alignment_bits)
        ]
        layout += [("base"+str(i), bus_aw + bus_alignment_bits, 0, bus_alignment_bits)
            for i in range(ndmas)]
        SingleGenerator.__init__(self, layout, MODE_CONTINUOUS)

    def dma_subr(self, i=0):
        return ["length", "base"+str(i)]


class TimingGenerator(Module):
    """Timing Generator

    Generates the horizontal / vertical video timings of a video frame.
    """
    def __init__(self):
        self.sink = sink = stream.Endpoint(frame_parameter_layout)
        self.source = source = stream.Endpoint(frame_synchro_layout)

        # # #

        hactive = Signal()
        vactive = Signal()
        active = Signal()

        hcounter = Signal(hbits)
        vcounter = Signal(vbits)

        self.comb += [
            If(sink.valid,
                active.eq(hactive & vactive),
                If(active,
                    source.valid.eq(1),
                    source.de.eq(1),
                ).Else(
                    source.valid.eq(1)
                )
            )
        ]

        self.sync += \
            If(sink.valid & source.ready,
                source.last.eq(0),
                hcounter.eq(hcounter + 1),

                If(hcounter == 0, hactive.eq(1)),
                If(hcounter == sink.hres, hactive.eq(0)),
                If(hcounter == sink.hsync_start, source.hsync.eq(1)),
                If(hcounter == sink.hsync_end, source.hsync.eq(0)),
                If(hcounter == sink.hscan,
                    hcounter.eq(0),
                    If(vcounter == sink.vscan,
                        vcounter.eq(0),
                        source.last.eq(1)
                    ).Else(
                        vcounter.eq(vcounter + 1)
                    )
                ),

                If(vcounter == 0, vactive.eq(1)),
                If(vcounter == sink.vres, vactive.eq(0)),
                If(vcounter == sink.vsync_start, source.vsync.eq(1)),
                If(vcounter == sink.vsync_end, source.vsync.eq(0))
            )
        self.comb += sink.ready.eq(source.ready & source.last)


clocking_cls = {
    "xc6" : S6HDMIOutClocking
}

phy_cls = {
    "xc6" : S6HDMIOutPHY
}

class Driver(Module, AutoCSR):
    """Driver

    Low level video interface module.
    """
    def __init__(self, device, pack_factor, pads, external_clocking=None):
        self.sink = stream.Endpoint(phy_description(pack_factor))

        # # #

        family = device[:3]

        self.submodules.clocking = clocking_cls[family](pads, external_clocking)

        # fifo / cdc
        fifo = stream.AsyncFIFO(phy_description(pack_factor), 512)
        fifo = ClockDomainsRenamer({"write": "sys", "read": "pix"})(fifo)
        self.submodules += fifo
        converter = stream.StrideConverter(phy_description(pack_factor),
                                           phy_description(1))
        converter = ClockDomainsRenamer("pix")(converter)
        self.submodules += converter
        self.comb += [
            self.sink.connect(fifo.sink),
            fifo.source.connect(converter.sink),
            converter.source.ready.eq(1)
        ]

        # ycbcr422 --> rgb444
        chroma_upsampler = YCbCr422to444()
        self.submodules += ClockDomainsRenamer("pix")(chroma_upsampler)
        self.comb += [
          chroma_upsampler.sink.valid.eq(converter.source.de),
          chroma_upsampler.sink.y.eq(converter.source.data[8:]),
          chroma_upsampler.sink.cb_cr.eq(converter.source.data[:8])
        ]

        ycbcr2rgb = YCbCr2RGB()
        self.submodules += ClockDomainsRenamer("pix")(ycbcr2rgb)
        self.comb += [
            chroma_upsampler.source.connect(ycbcr2rgb.sink),
            ycbcr2rgb.source.ready.eq(1)
        ]

        de = converter.source.de
        hsync = converter.source.hsync
        vsync = converter.source.vsync
        for i in range(chroma_upsampler.latency +
                       ycbcr2rgb.latency):
            next_de = Signal()
            next_vsync = Signal()
            next_hsync = Signal()
            self.sync.pix += [
                next_de.eq(de),
                next_vsync.eq(vsync),
                next_hsync.eq(hsync),
            ]
            de = next_de
            vsync = next_vsync
            hsync = next_hsync

        # phy
        self.submodules.hdmi_phy = phy_cls[family](self.clocking.serdesstrobe, pads)
        self.comb += [
            self.hdmi_phy.hsync.eq(hsync),
            self.hdmi_phy.vsync.eq(vsync),
            self.hdmi_phy.de.eq(de),
            self.hdmi_phy.r.eq(ycbcr2rgb.source.r),
            self.hdmi_phy.g.eq(ycbcr2rgb.source.g),
            self.hdmi_phy.b.eq(ycbcr2rgb.source.b)
        ]


class VideoOutCore(Module, AutoCSR):
    """Video out core

    Generates a video stream from memory.
    """
    def __init__(self, lasmim):
        self.pack_factor = lasmim.dw//bpp
        self.source = stream.Endpoint(phy_description(self.pack_factor))

        # # #

        self.submodules.fi = fi = FrameInitiator(lasmim.aw, self.pack_factor)
        self.submodules.intseq = intseq = IntSequence(lasmim.aw, lasmim.aw)
        self.submodules.dma_reader = dma_reader = dma_lasmi.Reader(lasmim)
        self.submodules.cast = cast = stream.Cast(lasmim.dw,
                                                  pixel_layout(self.pack_factor),
                                                  reverse_to=True)
        self.submodules.vtg = vtg = TimingGenerator(self.pack_factor)

        self.comb += [
            # fi --> intseq
            intseq.sink.valid.eq(fi.source.valid),
            intseq.sink.offset.eq(fi.source.base0),
            intseq.sink.maximum.eq(fi.source.length),

            # fi --> vtg
            vtg.timing.valid.eq(fi.source.valid),
            vtg.timing.hres.eq(fi.source.hres),
            vtg.timing.hsync_start.eq(fi.source.hsync_start),
            vtg.timing.hsync_end.eq(fi.source.hsync_end),
            vtg.timing.hscan.eq(fi.source.hscan),
            vtg.timing.vres.eq(fi.source.vres),
            vtg.timing.vsync_start.eq(fi.source.vsync_start),
            vtg.timing.vsync_end.eq(fi.source.vsync_end),
            vtg.timing.vscan.eq(fi.source.vscan),

            fi.source.ready.eq(vtg.timing.ready),

            # intseq --> dma_reader
            dma_reader.sink.valid.eq(intseq.source.valid),
            dma_reader.sink.address.eq(intseq.source.value),
            intseq.source.ready.eq(dma_reader.sink.ready),

            # dma_reader --> cast
            cast.sink.valid.eq(dma_reader.source.valid),
            cast.sink.payload.raw_bits().eq(dma_reader.source.data),
            dma_reader.source.ready.eq(cast.sink.ready),

            # cast --> vtg
            vtg.pixels.valid.eq(cast.source.valid),
            vtg.pixels.payload.eq(cast.source.payload),
            cast.source.ready.eq(vtg.pixels.ready),

            # vtg --> source
            vtg.phy.connect(self.source)
        ]


class VideoOut(Module, AutoCSR):
    """Video out

    Generates a video from memory.
    """
    def __init__(self, device, pads, lasmim, external_clocking=None):
        self.submodules.core = VideoOutCore(lasmim)
        self.submodules.driver = Driver(device,
                                        self.core.pack_factor,
                                        pads,
                                        external_clocking)
        self.comb += self.core.source.connect(self.driver.sink)
