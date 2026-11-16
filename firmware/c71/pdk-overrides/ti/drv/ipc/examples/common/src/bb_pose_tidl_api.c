
#include <string.h>
#include "itidl_ti.h"

const IVISION_Fxns *bb_pose_tidl_functions(void)
{
    return &TIDL_VISION_FXNS;
}

int bb_pose_tidl_defaults(TIDL_CreateParams *p,
                         sTIDL_Network_t *network,
                         void *udma)
{
    int status;
    memset(p, 0, sizeof(*p));
    status = TIDL_createParamsInit(p);
    p->net = network;
    p->udmaDrvObj = udma;
    return status;
}
