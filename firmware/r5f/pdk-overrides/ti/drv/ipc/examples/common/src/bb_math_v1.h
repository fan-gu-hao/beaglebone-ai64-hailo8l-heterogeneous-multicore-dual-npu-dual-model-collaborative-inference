
#ifndef BB_MATH_V1_H
#define BB_MATH_V1_H
#include <stdint.h>
#include <string.h>

#define BB_MAGIC 0x31544d42u
#define BB_TOKEN 3542211769u

static volatile const char bb_build_id[] = "BBAI64_MATH_0b7e1edcc4abb61d";

static uint32_t bb_get(const unsigned char *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void bb_put(unsigned char *p, uint32_t v)
{
    p[0] = (unsigned char)v;
    p[1] = (unsigned char)(v >> 8);
    p[2] = (unsigned char)(v >> 16);
    p[3] = (unsigned char)(v >> 24);
}

static float bb_float(const unsigned char *p)
{
    uint32_t v = bb_get(p);
    float f;
    memcpy(&f, &v, 4);
    return f;
}

static void bb_store_float(unsigned char *p, float f)
{
    uint32_t v;
    memcpy(&v, &f, 4);
    bb_put(p, v);
}

static uint16_t bb_compute(char *buffer, uint16_t len)
{
    unsigned char *p = (unsigned char *)buffer;
    uint32_t op, i, j, k, a, b;
    float x[9], y[9], sum;
    uint16_t result;

    if (bb_build_id[0] != 'B' || len < 16 || bb_get(p) != BB_MAGIC)
        return 0;

    op = bb_get(p + 4);

    if (bb_get(p + 12) != BB_TOKEN ||
        !(((op == 1 || op == 2) && len == 80) ||
          (op == 3 && len == 88))) {
        bb_put(p + 4, op | 0xc0000000u);
        return 16;
    }

    if (op == 1) {
        for (i = 0; i < 8; i++) {
            a = bb_get(p + 16 + 4*i);
            b = bb_get(p + 48 + 4*i);
            bb_put(p + 16 + 4*i, a*b + 7u);
        }
        result = 48;
    } else if (op == 2) {
        for (i = 0; i < 8; i++) {
            sum = bb_float(p + 16 + 4*i) *
                  bb_float(p + 48 + 4*i) + 0.25f;
            bb_store_float(p + 16 + 4*i, sum);
        }
        result = 48;
    } else {
        for (i = 0; i < 9; i++) {
            x[i] = bb_float(p + 16 + 4*i);
            y[i] = bb_float(p + 52 + 4*i);
        }

        for (i = 0; i < 3; i++) {
            for (j = 0; j < 3; j++) {
                sum = 0.0f;
                for (k = 0; k < 3; k++)
                    sum += x[3*i + k] * y[3*k + j];
                bb_store_float(p + 16 + 4*(3*i + j), sum);
            }
        }
        result = 52;
    }

    bb_put(p + 4, op | 0x80000000u);
    return result;
}
#endif
