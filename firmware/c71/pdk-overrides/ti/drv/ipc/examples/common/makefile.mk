#
# This file is the makefile for building IPC example app for TI RTOS
#
SRCDIR += . ../common/src
INCDIR =

# List all the external components/interfaces, whose interface header files
#  need to be included for this component
INCLUDE_EXTERNAL_INTERFACES = pdk

# Common source files and CFLAGS across all platforms and cores
PACKAGE_SRCS_COMMON = . ../common ../../common

# List all the components required by the application
ifeq ($(BUILD_OS_TYPE), baremetal)
  COMP_LIST_COMMON =  $(PDK_COMMON_BAREMETAL_COMP)
  SRCS_COMMON += main_baremetal.c ipc_testsetup_baremetal.c
  SRCS_COMMON += ipc_trace.c
  COMP_LIST_COMMON += ipc_baremetal
  CFLAGS_LOCAL_COMMON += -DIPC_EXCLUDE_CTRL_TASKS -DBAREMETAL
  ifeq ($(ISA),$(filter $(ISA), a53 a72))
    LNKFLAGS_LOCAL_$(CORE) += --entry Entry
  endif
  ifeq ($(ISA), r5f)
	  SRCS_COMMON += r5f_mpu_$(SOC)_default.c
  endif
  ifeq ($(SOC),$(filter $(SOC), j721e j7200 am65xx am64x))
    EXTERNAL_LNKCMD_FILE_LOCAL = $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/linker_$(ISA)_$(CORE).lds
    ifeq ($(ECHO_TEST_BTCM), 1)
      ifeq ($(ISA), r5f)
        ifeq ($(CORE),mcu1_0)
	        EXTERNAL_LNKCMD_FILE_LOCAL = $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/linker_$(ISA)_$(CORE)_btcm.lds
        endif
      endif
    endif
  endif
endif
ifeq ($(BUILD_OS_TYPE), tirtos)
  COMP_LIST_COMMON = $(PDK_COMMON_TIRTOS_COMP)
  COMP_LIST_COMMON += ipc 
  ifeq ($(SOC),$(filter $(SOC), j721e j7200))
    ifeq ($(CORE),mcu1_0)
      COMP_LIST_COMMON += sciserver_tirtos
    endif
  endif
  SRCS_COMMON += main_tirtos.c ipc_testsetup.c
  SRCS_COMMON += ipc_utils.c
  CFLAGS_LOCAL_COMMON += -DSYSBIOS
  INCLUDE_EXTERNAL_INTERFACES += xdc bios
  # Enable XDC build for application by providing XDC CFG File per core
  XDC_CFG_FILE_$(CORE) = $(PDK_INSTALL_PATH)/ti/build/$(SOC)/sysbios_$(ISA).cfg
  ifeq ($(SOC),$(filter $(SOC), j721e j7200))
    XDC_CFG_UPDATE_$(CORE) = $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/ipc_override.cfg
    ifeq ($(ECHO_TEST_BTCM), 1)
      ifeq ($(ISA), r5f)
        ifeq ($(CORE),mcu1_0)
	        XDC_CFG_FILE_$(CORE) = $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/sysbios_$(ISA)_btcm.cfg
	        SRCS_ASM_COMMON += ipcCopyVecs2Exc.asm
        else
	        XDC_CFG_FILE_$(CORE) = $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/sysbios_$(ISA).cfg
        endif
	      EXTERNAL_LNKCMD_FILE_LOCAL = $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/linker_$(ISA)_$(CORE)_btcm_sysbios.lds
      else
	      EXTERNAL_LNKCMD_FILE_LOCAL = $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/linker_$(ISA)_$(CORE)_sysbios.lds
      endif
    else
      ifeq ($(ISA), r5f)
	      XDC_CFG_FILE_$(CORE) = $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/sysbios_$(ISA).cfg
      endif
	    EXTERNAL_LNKCMD_FILE_LOCAL = $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/linker_$(ISA)_$(CORE)_sysbios.lds
    endif
  endif
  ifeq ($(SOC),$(filter $(SOC), am65xx am64x))
    XDC_CFG_UPDATE_$(CORE) = $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/ipc_override.cfg
    ifeq ($(ISA), r5f)
      XDC_CFG_FILE_$(CORE) = $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/sysbios_$(ISA).cfg
    endif
    EXTERNAL_LNKCMD_FILE_LOCAL = $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/linker_$(ISA)_$(CORE)_sysbios.lds
  endif
endif
ifeq ($(BUILD_OS_TYPE), freertos)
  COMP_LIST_COMMON =  $(PDK_COMMON_FREERTOS_COMP)
  COMP_LIST_COMMON += ipc
  ifeq ($(SOC),$(filter $(SOC), j721e j7200))
    ifeq ($(CORE),mcu1_0)
      COMP_LIST_COMMON += sciserver_tirtos
    endif
  endif
  SRCS_COMMON += main_tirtos.c ipc_testsetup.c
  SRCS_COMMON += ipc_trace.c
  CFLAGS_LOCAL_COMMON += -DFREERTOS
  INCLUDE_EXTERNAL_INTERFACES += freertos
  ifeq ($(ISA), r5f)
	  SRCS_COMMON += r5f_mpu_$(SOC)_default.c
  endif
  EXTERNAL_LNKCMD_FILE_LOCAL = $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/$(BUILD_OS_TYPE)/linker_$(ISA)_$(CORE)_$(BUILD_OS_TYPE).lds
  APPEND_LNKCMD_FILE += $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/$(BUILD_OS_TYPE)/memory_map_ddr.cmd
  ifeq ($(ECHO_TEST_BTCM), 1)
      EXTERNAL_LNKCMD_FILE_LOCAL = $(PDK_INSTALL_PATH)/ti/drv/ipc/examples/common/$(SOC)/$(BUILD_OS_TYPE)/linker_$(ISA)_$(CORE)_btcm_$(BUILD_OS_TYPE).lds
  endif
endif

ifeq ($(SOC), am64x)
  COMP_LIST_COMMON += mailbox
endif

CFLAGS_LOCAL_COMMON += $(PDK_CFLAGS)

# Core/SoC/platform specific source files and CFLAGS
# Example:
#   SRCS_<core/SoC/platform-name> =
#   CFLAGS_LOCAL_<core/SoC/platform-name> =

# Include common make files
ifeq ($(MAKERULEDIR), )
#Makerule path not defined, define this and assume relative path from ROOTDIR
  MAKERULEDIR := $(ROOTDIR)/ti/build/makerules
  export MAKERULEDIR
endif


# BEGIN BB_POSE_TIDL_LINK_V1
ifeq ($(CORE),c7x_1)
ifeq ($(BUILD_OS_TYPE),tirtos)
SRCS_COMMON += bb_pose_tidl_api.c
INCDIR += /home/fan/ti/bbai64-bringup/sdk-08.00.00.12/ti-processor-sdk-rtos-j721e-evm-08_00_00_12/ti-cgt-c7000_1.4.2.LTS/include /home/fan/ti/bbai64-bringup/sdk-08.00.00.12/ti-processor-sdk-rtos-j721e-evm-08_00_00_12/tidl_j7_08_00_00_10/ti_dl/inc /home/fan/ti/bbai64-bringup/sdk-08.00.00.12/ti-processor-sdk-rtos-j721e-evm-08_00_00_12/ivision /home/fan/ti/bbai64-bringup/sdk-08.00.00.12/ti-processor-sdk-rtos-j721e-evm-08_00_00_12/xdais_7_24_00_04/packages /home/fan/ti/bbai64-bringup/sdk-08.00.00.12/ti-processor-sdk-rtos-j721e-evm-08_00_00_12/xdctools_3_61_04_40_core/packages
LNKFLAGS_LOCAL_c7x_1 += --reread_libs --undef_sym=TIDL_VISION_FXNS --undef_sym=bb_pose_tidl_defaults
LNKFLAGS_LOCAL_c7x_1 += --library=/home/fan/ti/bbai64-bringup/sdk-08.00.00.12/ti-processor-sdk-rtos-j721e-evm-08_00_00_12/tidl_j7_08_00_00_10/ti_dl/lib/dsp/algo/release/tidl_algo.lib --library=/home/fan/ti/bbai64-bringup/sdk-08.00.00.12/ti-processor-sdk-rtos-j721e-evm-08_00_00_12/tidl_j7_08_00_00_10/ti_dl/lib/dsp/algo/release/tidl_obj_algo.lib --library=/home/fan/ti/bbai64-bringup/sdk-08.00.00.12/ti-processor-sdk-rtos-j721e-evm-08_00_00_12/tidl_j7_08_00_00_10/ti_dl/lib/dsp/algo/release/tidl_priv_algo.lib --library=/home/fan/ti/bbai64-bringup/sdk-08.00.00.12/ti-processor-sdk-rtos-j721e-evm-08_00_00_12/tidl_j7_08_00_00_10/ti_dl/lib/dsp/algo/release/tidl_custom.lib --library=/home/fan/ti/bbai64-bringup/sdk-08.00.00.12/ti-processor-sdk-rtos-j721e-evm-08_00_00_12/mmalib_02_01_00_07/lib/release/common_C7100.lib --library=/home/fan/ti/bbai64-bringup/sdk-08.00.00.12/ti-processor-sdk-rtos-j721e-evm-08_00_00_12/mmalib_02_01_00_07/lib/release/mmalib_C7100.lib --library=/home/fan/ti/bbai64-bringup/sdk-08.00.00.12/ti-processor-sdk-rtos-j721e-evm-08_00_00_12/mmalib_02_01_00_07/lib/release/mmalib_cn_C7100.lib --library=/home/fan/ti/bbai64-bringup/pose-mobilenetv2-v1/runtime/c71.02bz9bq0/pdk/packages/ti/drv/udma/lib/j721e/c7x_1/release/udma.ae71 --library=/home/fan/ti/bbai64-bringup/pose-mobilenetv2-v1/runtime/c71.02bz9bq0/pdk/packages/ti/drv/udma/lib/j721e/c7x_1/release/dmautils.ae71
endif
endif
# END BB_POSE_TIDL_LINK_V1

# BEGIN BB_POSE_SELFTEST_V1
ifeq ($(CORE),c7x_1)
SRCS_COMMON += bb_pose_service.c bb_pose_assets.c
INCDIR += /home/fan/ti/bbai64-bringup/sdk-08.00.00.12/ti-processor-sdk-rtos-j721e-evm-08_00_00_12/tidl_j7_08_00_00_10/ti_dl/custom
LNKFLAGS_LOCAL_c7x_1 += --undef_sym=bb_pose_dispatch --undef_sym=bb_pose_early
endif
# END BB_POSE_SELFTEST_V1

include $(MAKERULEDIR)/common.mk

# OBJs and libraries are built by using rule defined in rules_<target>.mk
#     and need not be explicitly specified here

# Nothing beyond this point
