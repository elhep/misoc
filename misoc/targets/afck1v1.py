#!/usr/bin/env python3

import argparse

from migen import *
from migen.build.platforms import afck1v1
from migen.genlib.resetsync import AsyncResetSynchronizer

from misoc.cores.liteeth_mini.mac import LiteEthMAC
from misoc.cores.liteeth_mini.phy.k7_1000basex import K7_1000BASEX
from misoc.cores import spi_flash

from misoc.cores.sdram_settings import MT41K512M8
from misoc.cores.sdram_phy import k7ddrphy
from misoc.integration.soc_sdram import *
from misoc.integration.builder import *


# CRG ----------------------------------------------------------------------------------------------

class _CRG(Module):
    def __init__(self, platform):
        self.clock_domains.cd_sys = ClockDomain()
        self.clock_domains.cd_sys4x = ClockDomain(reset_less=True)
        self.clock_domains.cd_sys4x_dqs = ClockDomain(reset_less=True)
        self.clock_domains.cd_clk200 = ClockDomain()
        clk125 = platform.request("fpga_clk")  #TODO: Check with schematics
        platform.add_period_constraint(clk125, 8.)
        self.clk125_buf = Signal()
        self.specials += [
            Instance("IBUFGDS",
                     p_DIFF_TERM="TRUE", p_IBUF_LOW_PWR="TRUE",
                     i_I=clk125.p, i_IB=clk125.n, o_O=self.clk125_buf),
        ]
        self.mmcm_locked = mmcm_locked = Signal()
        mmcm_fb = Signal()
        mmcm_sys = Signal()
        mmcm_sys4x = Signal()
        mmcm_sys4x_dqs = Signal()
        mmcm_clk200 = Signal()
        self.specials += [
            Instance("MMCME2_BASE",
                p_CLKIN1_PERIOD=8.0,
                i_CLKIN1=self.clk125_buf,

                i_CLKFBIN=mmcm_fb,
                o_CLKFBOUT=mmcm_fb,
                o_LOCKED=mmcm_locked,

                # VCO @ 1GHz with MULT=16
                p_CLKFBOUT_MULT_F=8, p_DIVCLK_DIVIDE=1,

                # ~125MHz
                p_CLKOUT0_DIVIDE_F=8.0, p_CLKOUT0_PHASE=0.0, o_CLKOUT0=mmcm_sys,

                # ~500MHz. Must be more than 400MHz as per DDR3 specs.
                p_CLKOUT1_DIVIDE=2, p_CLKOUT1_PHASE=0.0, o_CLKOUT1=mmcm_sys4x,

                # ~200MHz for IDELAYCTRL. Datasheet specified tolerance +/- 10MHz.
                p_CLKOUT2_DIVIDE=5, p_CLKOUT2_PHASE=0.0, o_CLKOUT2=mmcm_clk200,

                p_CLKOUT3_DIVIDE=2, p_CLKOUT3_PHASE=90.0, o_CLKOUT3=mmcm_sys4x_dqs,
            ),
            Instance("BUFG", i_I=mmcm_sys, o_O=self.cd_sys.clk),
            Instance("BUFG", i_I=mmcm_sys4x, o_O=self.cd_sys4x.clk),
            Instance("BUFG", i_I=mmcm_sys4x_dqs, o_O=self.cd_sys4x_dqs.clk),
            Instance("BUFG", i_I=mmcm_clk200, o_O=self.cd_clk200.clk),
            AsyncResetSynchronizer(self.cd_sys, ~mmcm_locked),
            AsyncResetSynchronizer(self.cd_clk200, ~mmcm_locked),
        ]

        reset_counter = Signal(4, reset=15)
        ic_reset = Signal(reset=1)
        self.sync.clk200 += \
            If(reset_counter != 0,
                reset_counter.eq(reset_counter - 1)
            ).Else(
                ic_reset.eq(0)
            )
        self.specials += Instance("IDELAYCTRL", i_REFCLK=ClockSignal("clk200"), i_RST=ic_reset)

        platform.add_period_constraint(self.cd_sys.clk, 8.)

# BaseSoC ------------------------------------------------------------------------------------------

class BaseSoC(SoCSDRAM):

    def __init__(self, sdram_controller_type="minicon", crg=None, **kwargs):
        platform = afck1v1.Platform()

        SoCSDRAM.__init__(self, platform, 
                          clk_freq=125000000,
                          **kwargs)
        if crg is None:
            self.submodules.crg = _CRG(platform)
        else:
            self.submodules.crg = crg(platform)

        # sdram
        self.submodules.ddrphy = k7ddrphy.K7DDRPHY(platform.request("ddram"))
        sdram_module = MT41K512M8(self.clk_freq, "1:4")
        self.register_sdram(self.ddrphy,
                            sdram_controller_type,
                            sdram_module.geom_settings,
                            sdram_module.timing_settings)
        self.csr_devices += ["ddrphy"]

        if not self.integrated_rom_size:
            spiflash_pads = platform.request("spiflash1x")
            spiflash_pads.clk = Signal()
            self.specials += Instance("STARTUPE2",
                                  i_CLK=0, i_GSR=0, i_GTS=0, i_KEYCLEARB=0, i_PACK=0,
                                  i_USRCCLKO=spiflash_pads.clk, i_USRCCLKTS=0, i_USRDONEO=1, i_USRDONETS=1)
            self.submodules.spiflash = spi_flash.SpiFlashSingle(spiflash_pads, dummy=8, div=7)
            self.config["SPIFLASH_PAGE_SIZE"] = 256
            self.config["SPIFLASH_SECTOR_SIZE"] = 0x40000

            self.register_rom(self.spiflash.bus, 16 * 1024 * 1024)
            self.csr_devices.append("spiflash")
            self.flash_boot_address = 0x880000

# EthernetSoC ------------------------------------------------------------------------------------------

class MiniSoC(BaseSoC):
    mem_map = {
        "ethmac": 0x30000000,  # (shadow @0xb0000000)
    }
    mem_map.update(BaseSoC.mem_map)

    def __init__(self, ethmac_nrxslots=2, ethmac_ntxslots=2, **kwargs):
        BaseSoC.__init__(self, **kwargs)

        self.submodules.ethphy = K7_1000BASEX(self.platform.request("mgt116_clk1"), self.platform.request("mgt116", 3), 125e6)
        self.platform.add_period_constraint(self.platform.lookup_request("mgt116_clk1"), 1e9 / 125e6)
        self.csr_devices += ["ethphy"]
        
        self.submodules.ethmac = LiteEthMAC(
            phy=self.ethphy, 
            dw=32,
            interface="wishbone",
            nrxslots=2, 
            ntxslots=2)
        ethmac_len = (ethmac_nrxslots + ethmac_ntxslots) * 0x800
        self.add_wb_slave(self.mem_map["ethmac"], ethmac_len, self.ethmac.bus)
        self.add_memory_region("ethmac",
                self.mem_map["ethmac"] | self.shadow_base, ethmac_len)
        self.csr_devices += ["ethmac"]
        self.interrupt_devices.append("ethmac")

        self.ethphy.cd_eth_rx.clk.attr.add("keep")
        self.ethphy.cd_eth_tx.clk.attr.add("keep")
        self.ethphy.txoutclk.attr.add("keep")
        self.ethphy.rxoutclk.attr.add("keep")
        self.platform.add_period_constraint(self.ethphy.cd_eth_rx.clk, 1e9 / 125e6)
        self.platform.add_period_constraint(self.ethphy.cd_eth_tx.clk, 1e9 / 125e6)
        self.platform.add_period_constraint(self.ethphy.txoutclk, 1e9 / 125e6 * 2)
        self.platform.add_period_constraint(self.ethphy.rxoutclk, 1e9 / 125e6 * 2)
        self.platform.add_false_path_constraints(
            self.crg.cd_sys.clk,
            self.ethphy.cd_eth_rx.clk,
            self.ethphy.cd_eth_tx.clk)


def soc_afck1v1_args(parser):
    soc_sdram_args(parser)
    parser.add_argument("--with-spi-flash", action="store_false",
                        help="enable SPI Flash support ")


def soc_afck1v1_argdict(args):
    r = soc_sdram_argdict(args)
    return r

# Build --------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="MiSoC port to AFCK 1v1")
    builder_args(parser)
    soc_afck1v1_args(parser)
    parser.add_argument("--with-ethernet", action="store_true",
                        help="enable Ethernet support")
    args = parser.parse_args()

    cls = MiniSoC if args.with_ethernet else BaseSoC
    soc = cls(**soc_afck1v1_argdict(args))
    builder = Builder(soc, **builder_argdict(args))
    builder.build()


if __name__ == "__main__":
    main()
