
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include "itidl_ti.h"

static void load(const char *name, void *p, size_t size)
{
    FILE *f = fopen(name, "rb");
    if (!f || fread(p, 1, size, f) != size) {
        fprintf(stderr, "Cannot read model header: %s\n", name);
        exit(2);
    }
    fclose(f);
}

int main(int argc, char **argv)
{
    if (argc != 3) return 2;

    sTIDL_Network_t *n = calloc(1, sizeof(*n));
    sTIDL_IOBufDesc_t *d = calloc(1, sizeof(*d));
    if (!n || !d) return 2;

    load(argv[1], n, sizeof(*n));
    load(argv[2], d, sizeof(*d));

    if (n->netVersion != TIDL_NET_VERSION ||
        d->numInputBuf != 1 || d->numOutputBuf != 1) {
        fprintf(stderr,
                "Version or tensor count mismatch: %x %d %d\n",
                n->netVersion, d->numInputBuf, d->numOutputBuf);
        return 3;
    }

    printf("{\n\"net_version\":\"0x%08x\",\"layers\":%d,"
           "\"dataflow_offset\":%d,\"network_header_bytes\":%zu,"
           "\"io_header_bytes\":%zu,\n",
           n->netVersion, n->numLayers, n->dataFlowInfo,
           sizeof(*n), sizeof(*d));

    printf("\"input\":{\"shape\":[%d,%d,%d,%d],\"type\":%d,"
           "\"layout\":%d,\"pitch\":%d,\"pad_ltrb\":[%d,%d,%d,%d],"
           "\"scale\":%.9g,\"zero_point\":%d,\"data_id\":%d},\n",
           d->inNumBatches[0], d->inNumChannels[0],
           d->inHeight[0], d->inWidth[0],
           d->inElementType[0], d->inLayout[0],
           d->inChannelPitch[0],
           d->inPadL[0], d->inPadT[0],
           d->inPadR[0], d->inPadB[0],
           d->inTensorScale[0], d->inZeroPoint[0],
           d->inDataId[0]);

    printf("\"output\":{\"shape\":[%d,%d,%d,%d],\"type\":%d,"
           "\"layout\":%d,\"pitch\":%d,\"pad_ltrb\":[%d,%d,%d,%d],"
           "\"scale\":%.9g,\"zero_point\":%d,\"data_id\":%d}}\n",
           d->outNumBatches[0], d->outNumChannels[0],
           d->outHeight[0], d->outWidth[0],
           d->outElementType[0], d->outLayout[0],
           d->outChannelPitch[0],
           d->outPadL[0], d->outPadT[0],
           d->outPadR[0], d->outPadB[0],
           d->outTensorScale[0], d->outZeroPoint[0],
           d->outDataId[0]);

    free(n);
    free(d);
    return 0;
}
