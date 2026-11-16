/* BB_V2_INPUT_SIZE_FIX */
#ifndef BB_INPUT_BYTES
#define BB_INPUT_BYTES (3U * 256U * 192U)
#endif
#if BB_INPUT_BYTES != 147456U
#error "Pose input must contain 147456 bytes"
#endif
/* 影像傳輸介面；TI 推論引擎保留在 bb_pose_service.c。 */
static uint8_t *bb_upload;
static uint32_t bb_upload_id, bb_upload_next, bb_upload_crc, bb_expected_crc;
static uint32_t bb_result_id;
static int bb_receiving;

static uint32_t bb_crc32(uint32_t crc, const uint8_t *data, uint32_t count)
{
    uint32_t i, bit;
    for (i=0; i<count; i++) {
        crc^=data[i];
        for (bit=0; bit<8; bit++)
            crc=(crc>>1)^((crc&1U)?0xEDB88320U:0U);
    }
    return crc;
}

static uint16_t bb_reply_error(char *msg, int code)
{
    int n=snprintf(msg,255,"POSE,ERROR,%s,%d",bb_stage,code);
    return (uint16_t)(n>0 && n<255?n:0);
}

static uint16_t bb_reply_done(char *msg)
{
    int n=snprintf(msg,255,"POSE,DONE,%u,%u,%.9g,%llu",
        bb_result_id,BB_OUTPUT_BYTES,(double)bb_scale,
        (unsigned long long)bb_cycles);
    return (uint16_t)(n>0 && n<255?n:0);
}

static uint16_t bb_reply_keys(char *msg)
{
    uint32_t joint,i,best,x,y;
    int peak,dx,dy;
    float points[51];
    const int8_t *map;

    for(joint=0;joint<17U;joint++) {
        map=(const int8_t *)bb_packed_out+joint*3072U;
        best=0;peak=map[0];

        for(i=1;i<3072U;i++) if((int)map[i]>peak) {
            peak=map[i];best=i;
        }

        x=best%48U;y=best/48U;
        points[joint*3U]=-1.0f;
        points[joint*3U+1U]=-1.0f;
        points[joint*3U+2U]=(float)peak/bb_scale;

        if(peak>0) {
            points[joint*3U]=(float)x*4.0f;
            points[joint*3U+1U]=(float)y*4.0f;

            if(x>1U && x<47U && y>1U && y<63U) {
                dx=(int)map[best+1U]-(int)map[best-1U];
                dy=(int)map[best+48U]-(int)map[best-48U];
                points[joint*3U]+=(float)((dx>0)-(dx<0));
                points[joint*3U+1U]+=(float)((dy>0)-(dy<0));
            }
        }
    }

    memcpy(msg,"PKP2",4);
    memcpy(msg+4,&bb_result_id,4);
    memcpy(msg+8,&bb_cycles,8);
    memcpy(msg+16,&bb_scale,4);
    memcpy(msg+20,points,sizeof(points));
    return (uint16_t)(20U+sizeof(points));
}

uint16_t bb_pose_dispatch(char *msg,uint16_t length)
{
    uint32_t id,offset,count,total,checksum;
    int status,n;
    char tail;

    if(length>=4U && memcmp(msg,"PIN2",4)==0) {
        if(length<=12U || length>252U)
            return bb_reply_error(msg,-201);

        memcpy(&id,msg+4,4);
        memcpy(&offset,msg+8,4);
        count=(uint32_t)length-12U;

        if(!bb_receiving || id!=bb_upload_id || offset!=bb_upload_next ||
           offset>BB_INPUT_BYTES || count>BB_INPUT_BYTES-offset)
            return bb_reply_error(msg,-202);

        memcpy(bb_upload+offset,msg+12,count);
        bb_upload_crc=bb_crc32(
            bb_upload_crc,(const uint8_t *)msg+12,count);
        bb_upload_next+=count;

        memcpy(msg,"PIA2",4);
        memcpy(msg+4,&id,4);
        memcpy(msg+8,&bb_upload_next,4);
        return 12;
    }

    if(length<5U || memcmp(msg,"POSE,",5)!=0)
        return 0;

    if(strcmp(msg,"POSE,INFO")==0) {
        n=snprintf(msg,255,
            "POSE,INFO,C71_TIDL_POSE_V2,flow=0,state=%s,error=%d,model="
            BB_POSE_NETWORK_SHA,bb_stage,bb_error);
        return (uint16_t)(n>0 && n<255?n:0);
    }

    if(sscanf(msg,"POSE,BEGIN,%u,%u,%u%c",
              &id,&total,&checksum,&tail)==3) {
        if(id==0U || total!=BB_INPUT_BYTES)
            return bb_reply_error(msg,-203);

        bb_receiving=0;
        bb_valid=0;
        status=bb_init();

        if(status) {
            bb_error=status;
            return bb_reply_error(msg,status);
        }

        if(!bb_upload)
            bb_upload=bb_alloc(BB_INPUT_BYTES,128,-1);

        if(!bb_upload) {
            bb_error=-204;
            return bb_reply_error(msg,-204);
        }

        bb_upload_id=id;
        bb_upload_next=0;
        bb_upload_crc=0xFFFFFFFFU;
        bb_expected_crc=checksum;
        bb_receiving=1;

        n=snprintf(msg,255,"POSE,READY,%u,%u",id,BB_INPUT_BYTES);
        return (uint16_t)(n>0 && n<255?n:0);
    }

    if(sscanf(msg,"POSE,RUN,%u%c",&id,&tail)==1) {
        if(!bb_receiving || id!=bb_upload_id ||
           bb_upload_next!=BB_INPUT_BYTES)
            return bb_reply_error(msg,-205);

        bb_receiving=0;

        if((bb_upload_crc^0xFFFFFFFFU)!=bb_expected_crc)
            return bb_reply_error(msg,-206);

        status=bb_run(bb_upload);
        bb_error=status;

        if(status)
            return bb_reply_error(msg,status);

        bb_result_id=id;
        return bb_reply_done(msg);
    }

    if(strcmp(msg,"POSE,TEST")==0) {
        bb_receiving=0;
        status=bb_run(bb_pose_test_input);
        bb_error=status;

        if(status)
            return bb_reply_error(msg,status);

        bb_result_id=0;
        return bb_reply_done(msg);
    }

    if(sscanf(msg,"POSE,KEYS,%u%c",&id,&tail)==1) {
        if(!bb_valid || id!=bb_result_id)
            return bb_reply_error(msg,-207);

        return bb_reply_keys(msg);
    }

    if(sscanf(msg,"POSE,OUT,%u,%u%c",&id,&offset,&tail)==2) {
        if(!bb_valid || id!=bb_result_id || offset>=BB_OUTPUT_BYTES)
            return bb_reply_error(msg,-208);

        count=BB_OUTPUT_BYTES-offset;
        if(count>240U)
            count=240U;

        memcpy(msg,"POD2",4);
        memcpy(msg+4,&id,4);
        memcpy(msg+8,&offset,4);
        memcpy(msg+12,bb_packed_out+offset,count);
        return (uint16_t)(12U+count);
    }

    return bb_reply_error(msg,-209);
}
