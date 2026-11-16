#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <xdc/runtime/System.h>
#include <ti/sysbios/family/c7x/Cache.h>
#include <ti/csl/soc.h>
#include <ti/csl/csl_clec.h>
#include <ti/drv/udma/udma.h>
#include <ti/drv/ipc/examples/common/src/ipc_setup.h>
#include <ti/drv/ipc/ipc.h>
#include <c7x.h>
#include "itidl_ti.h"
#include "tidl_custom.h"
#include "bb_pose_assets.h"

#define BB_OUTPUT_BYTES (17U*64U*48U)
#define BB_POOL_BYTES (96U*1024U*1024U)
#define BB_MEMRECS 256

#pragma DATA_SECTION(bb_pool, ".bb_pose_heap")
#pragma DATA_ALIGN(bb_pool, 128)
static uint8_t bb_pool[BB_POOL_BYTES];

static uint32_t bb_used;
static uintptr_t bb_local_used[3];
static const uintptr_t bb_local_base[3] = {
    0x64E00000U, 0x64800000U, 0x70020000U
};
static const uintptr_t bb_local_size[3] = {
    0x4000U, 0x70000U, 0x7C8000U
};

static IALG_MemRec bb_records[BB_MEMRECS];
static TIDL_CreateParams bb_params;
static TIDL_InArgs bb_inargs;
static TIDL_outArgs bb_outargs;
static struct Udma_DrvObj bb_udma;
static IVISION_Handle bb_handle;
static IVISION_BufDesc bb_indesc, bb_outdesc;
static IVISION_BufDesc *bb_inlist[1], *bb_outlist[1];
static IVISION_InBufs bb_inbufs;
static IVISION_OutBufs bb_outbufs;
static uint8_t *bb_padded_in, *bb_padded_out, *bb_packed_out;
static uint32_t bb_inbytes, bb_outbytes;
static int bb_ready, bb_attempted, bb_valid, bb_error;
static const char *bb_stage = "boot";
static float bb_scale;
static uint64_t bb_cycles;

static void bb_mark(const char *stage)
{
    void *trace;
    bb_stage = stage;
    App_printf("POSE stage=%s\n", stage);
    trace = Ipc_getResourceTraceBufPtr();
    if (trace)
        Cache_wb(trace, 0x80000U, Cache_Type_ALLD, TRUE);
}

static void *bb_alloc(uintptr_t size, uintptr_t align, int region)
{
    uintptr_t base, used, capacity, start;
    if (align < 128U)
        align = 128U;
    if ((align & (align - 1U)) != 0U)
        return NULL;

    if (region < 0) {
        base = (uintptr_t)bb_pool;
        used = bb_used;
        capacity = BB_POOL_BYTES;
    } else {
        base = bb_local_base[region];
        used = bb_local_used[region];
        capacity = bb_local_size[region];
    }

    start = (base + used + align - 1U) & ~(align - 1U);
    if (start < base || start - base > capacity ||
        size > capacity - (start - base))
        return NULL;

    if (region < 0)
        bb_used = (uint32_t)(start - base + size);
    else
        bb_local_used[region] = start - base + size;

    return (void *)start;
}

static int bb_log(const char *format, va_list args)
{
    char text[256];
    int n = vsnprintf(text, sizeof(text), format, args);
    App_printf("%s", text);
    return n;
}

static void bb_udma_log(const char *text)
{
    App_printf("POSE UDMA: %s", text);
}

static uint64_t bb_phys(const void *p, uint32_t channel, void *data)
{
    (void)channel;
    (void)data;
    return (uint64_t)(uintptr_t)p;
}

void bb_pose_early(void)
{
    Cache_Size size;
    CSL_ClecEventConfig config;
    uint32_t i;

    size.l1pSize = Cache_L1Size_32K;
    size.l1dSize = Cache_L1Size_32K;
    size.l2Size = Cache_L2Size_64K;
    Cache_setSize(&size);

    memset(&config, 0, sizeof(config));
    config.secureClaimEnable = FALSE;
    config.evtSendEnable = TRUE;
    config.rtMap = CSL_CLEC_RTMAP_CPU_ALL;

    for (i = 0; i < 16; i++) {
        config.c7xEvtNum = 32U + i;
        CSL_clecConfigEvent(
            (CSL_CLEC_EVTRegs *)CSL_COMPUTE_CLUSTER0_CLEC_REGS_BASE,
            192U + i, &config);
    }
}

static void bb_desc(
    IVISION_BufDesc *d, void *p,
    uint32_t width, uint32_t height, uint32_t channels,
    uint32_t left, uint32_t top, uint32_t bottom, int id)
{
    memset(d, 0, sizeof(*d));
    d->numPlanes = 1;
    d->bufPlanes[0].buf = p;
    d->bufPlanes[0].width = width + left;
    d->bufPlanes[0].height = channels * (height + top + bottom);
    d->bufPlanes[0].frameROI.width = width;
    d->bufPlanes[0].frameROI.height = height;
    d->reserved[0] = id;
    d->bufferId = id;
}

static int bb_init(void)
{
    const sTIDL_IOBufDesc_t *io =
        (const sTIDL_IOBufDesc_t *)bb_pose_io;
    const sTIDL_Network_t *original =
        (const sTIDL_Network_t *)bb_pose_network;
    sTIDL_Network_t *net;
    Udma_InitPrms udma_params;
    IALG_Handle temporary = NULL;
    int count, i, status, region;

    if (bb_ready)
        return 0;
    if (bb_attempted)
        return bb_error ? bb_error : -100;

    bb_attempted = 1;
    bb_mark("model-interface");

    if (sizeof(*io) != BB_POSE_IO_SIZE ||
        sizeof(*original) != 3235916U ||
        original->netVersion != 0x20210723 ||
        original->numLayers != 69 ||
        original->dataFlowInfo <= 0 ||
        (uint32_t)original->dataFlowInfo >= BB_POSE_NETWORK_SIZE ||
        io->numInputBuf != 1 || io->numOutputBuf != 1 ||
        io->inNumBatches[0] != 1 || io->inNumChannels[0] != 3 ||
        io->inHeight[0] != 256 || io->inWidth[0] != 192 ||
        io->inElementType[0] != TIDL_UnsignedChar ||
        io->inLayout[0] != 0 ||
        io->inPadL[0] != 1 || io->inPadT[0] != 1 ||
        io->inPadR[0] != 0 || io->inPadB[0] != 2 ||
        io->inChannelPitch[0] != 49987 ||
        io->inTensorScale[0] != 1 || io->inZeroPoint[0] != 0 ||
        io->outNumBatches[0] != 1 || io->outNumChannels[0] != 17 ||
        io->outHeight[0] != 64 || io->outWidth[0] != 48 ||
        io->outElementType[0] != TIDL_SignedChar ||
        io->outLayout[0] != 0 ||
        io->outPadL[0] != 8 || io->outPadT[0] != 2 ||
        io->outPadR[0] != 0 || io->outPadB[0] != 24 ||
        io->outChannelPitch[0] != 5040 ||
        io->outZeroPoint[0] != 0 ||
        io->outPadCh[0] < 0 || io->outPadCh[0] > 64)
        return -101;

    bb_mark("network-copy");
    net = bb_alloc(BB_POSE_NETWORK_SIZE, 128, -1);
    if (!net)
        return -102;
    memcpy(net, original, BB_POSE_NETWORK_SIZE);

    bb_mark("udma-init");
    UdmaInitPrms_init(UDMA_INST_ID_MAIN_0, &udma_params);
    udma_params.skipGlobalEventReg = 1;
    udma_params.printFxn = bb_udma_log;
    udma_params.virtToPhyFxn = bb_phys;
    status = Udma_init(&bb_udma, &udma_params);
    if (status != 0)
        return status;

    memset(&bb_params, 0, sizeof(bb_params));
    TIDL_createParamsInit(&bb_params);
    bb_params.net = net;
    bb_params.isInbufsPaded = 1;
    bb_params.flowCtrl = 0;
    bb_params.udmaDrvObj = &bb_udma;
    bb_params.TIDLVprintf = bb_log;
    bb_params.TIDL_CustomLayerProcess = TIDL_customLayerProcess;

    bb_mark("algAlloc");
    count = TIDL_VISION_FXNS.ialg.algNumAlloc();
    if (count < 1 || count > BB_MEMRECS)
        return -103;

    status = TIDL_VISION_FXNS.ialg.algAlloc(
        (IALG_Params *)&bb_params, NULL, bb_records);
    if (status != IALG_EOK)
        return status;

    bb_mark("memory-records");
    for (i = 0; i < count; i++) {
        region = -1;
        if (bb_records[i].space == IALG_DARAM0)
            region = 0;
        else if (bb_records[i].space == IALG_DARAM1)
            region = 1;
        else if (bb_records[i].space == IALG_SARAM0)
            region = 2;
        else if (bb_records[i].space != IALG_EXTERNAL)
            return -104;

        if ((uintptr_t)bb_records[i].size > (uintptr_t)BB_POOL_BYTES ||
            (uintptr_t)bb_records[i].alignment > (uintptr_t)BB_POOL_BYTES)
            return -105;

        bb_records[i].base = bb_alloc(
            (uintptr_t)bb_records[i].size,
            (uintptr_t)bb_records[i].alignment, region);

        App_printf(
            "POSE mem[%d] space=%d bytes=%d base=%p\n",
            i, bb_records[i].space,
            bb_records[i].size, bb_records[i].base);

        if (!bb_records[i].base)
            return -106;
    }

    bb_mark("algInit");
    status = TIDL_VISION_FXNS.ialg.algInit(
        (IALG_Handle)&temporary, bb_records,
        NULL, (IALG_Params *)&bb_params);
    if (status != IALG_EOK)
        return status;

    bb_handle = (IVISION_Handle)bb_records[0].base;
    if (!bb_handle || !bb_handle->fxns ||
        !bb_handle->fxns->algProcess)
        return -107;

    bb_mark("input-output-buffers");
    bb_inbytes = 3U * 49987U;
    bb_outbytes = (17U + (uint32_t)io->outPadCh[0] + 1U) * 5040U;

    bb_padded_in = bb_alloc(bb_inbytes, 128, -1);
    bb_padded_out = bb_alloc(bb_outbytes, 128, -1);
    bb_packed_out = bb_alloc(BB_OUTPUT_BYTES, 128, -1);
    if (!bb_padded_in || !bb_padded_out || !bb_packed_out)
        return -108;

    bb_desc(&bb_indesc, bb_padded_in,
            192, 256, 3, 1, 1, 2, io->inDataId[0]);
    bb_desc(&bb_outdesc, bb_padded_out,
            48, 64, 17, 8, 2, 24, io->outDataId[0]);

    bb_inlist[0] = &bb_indesc;
    bb_outlist[0] = &bb_outdesc;

    bb_inbufs.size = sizeof(bb_inbufs);
    bb_inbufs.numBufs = 1;
    bb_inbufs.bufDesc = bb_inlist;

    bb_outbufs.size = sizeof(bb_outbufs);
    bb_outbufs.numBufs = 1;
    bb_outbufs.bufDesc = bb_outlist;

    bb_ready = 1;
    bb_mark("ready");
    return 0;
}

static int bb_run(const uint8_t *input)
{
    uint32_t c, y;
    uint64_t start;
    int status;

    bb_valid = 0;
    status = bb_init();
    if (status)
        return status;

    bb_mark("prepare-input");
    memset(bb_padded_in, 0, bb_inbytes);
    memset(bb_padded_out, 0, bb_outbytes);

    for (c = 0; c < 3; c++)
        for (y = 0; y < 256; y++)
            memcpy(
                bb_padded_in + c*49987U + (y+1U)*193U + 1U,
                input + c*256U*192U + y*192U,
                192U);

    memset(&bb_inargs, 0, sizeof(bb_inargs));
    memset(&bb_outargs, 0, sizeof(bb_outargs));
    bb_inargs.iVisionInArgs.size = sizeof(bb_inargs);
    bb_inargs.numInBufs = 1;
    bb_inargs.scale[0] = 1;
    bb_outargs.iVisionOutArgs.size = sizeof(bb_outargs);

    bb_mark("algActivate");
    if (bb_handle->fxns->ialg.algActivate)
        bb_handle->fxns->ialg.algActivate((IALG_Handle)bb_handle);

    Cache_wbInv(bb_pool, bb_used, Cache_Type_ALLD, TRUE);

    bb_mark("algProcess");
    App_printf(
        "POSE: TARGET DATA FLOW, flowCtrl=0, DDR=%u bytes\n",
        bb_used);

    start = __TSC;
    status = bb_handle->fxns->algProcess(
        bb_handle, &bb_inbufs, &bb_outbufs,
        (IVISION_InArgs *)&bb_inargs,
        (IVISION_OutArgs *)&bb_outargs);
    bb_cycles = __TSC - start;

    if (bb_handle->fxns->ialg.algDeactivate)
        bb_handle->fxns->ialg.algDeactivate((IALG_Handle)bb_handle);

    if (status != IALG_EOK)
        return status;

    bb_mark("unpack-output");
    bb_scale = bb_outargs.scale[0];
    if (!(bb_scale > 0.0f && bb_scale < 1.0e20f))
        return -109;

    Cache_wbInv(bb_padded_out, bb_outbytes, Cache_Type_ALLD, TRUE);

    for (c = 0; c < 17; c++)
        for (y = 0; y < 64; y++)
            memcpy(
                bb_packed_out + c*64U*48U + y*48U,
                bb_padded_out + c*5040U + (y+2U)*56U + 8U,
                48U);

    bb_valid = 1;
    bb_mark("done");
    return 0;
}

#include "bb_pose_transport_v2.inc"
