#ifndef BB_HX711_V1_H
#define BB_HX711_V1_H

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <ti/osal/HwiP.h>
#include <ti/osal/TaskP.h>
#include <ti/osal/TimerP.h>

#ifndef BUILD_MCU2_0
#error "HX711 firmware must be built for mcu2_0"
#endif

#ifndef HX_REG
#define HX_REG(a) (*(volatile uint32_t *)(uintptr_t)(a))
#endif

#define HX_DIR       0x00600038U
#define HX_OUT       0x0060003CU
#define HX_SET       0x00600040U
#define HX_CLR       0x00600044U
#define HX_IN        0x00600048U
#define HX_DATA_MASK (1U << 13)
#define HX_CLK_MASK  (1U << 15)
#define HX_PAD_DATA  0x0011C0B8U
#define HX_PAD_CLK   0x0011C0C0U

static uint32_t hx_initialized;
static uint32_t hx_sequence;
static uint32_t hx_busy;
static uint32_t hx_max_high_us;

static void hx_clock(uint32_t high)
{
    if (high) HX_REG(HX_SET) = HX_CLK_MASK;
    else HX_REG(HX_CLR) = HX_CLK_MASK;
    (void)HX_REG(HX_OUT);
}

/* 使用微秒時間；迴圈上限避免計時器停止時無限等待。 */
static int hx_delay_us(uint32_t delay)
{
    uint64_t start = TimerP_getTimeInUsecs();
    uint32_t count;

    for (count = 0U; count < 200000U; ++count) {
        if (TimerP_getTimeInUsecs() - start >= delay) return 0;
    }
    return -1;
}

static int hx_wait_ready(void)
{
    uint64_t start = TimerP_getTimeInUsecs();
    uint32_t count;

    for (count = 0U; count < 1500U; ++count) {
        if ((HX_REG(HX_IN) & HX_DATA_MASK) == 0U) return 0;
        if (TimerP_getTimeInUsecs() - start >= 1500000U) break;
        TaskP_sleep(1U);
    }
    return -2;
}

/* 高電位期間禁止中斷；離開前必定將時脈拉低。 */
static int hx_pulse(void)
{
    uintptr_t key = HwiP_disable();
    uint64_t start = TimerP_getTimeInUsecs();
    uint32_t elapsed;
    int ok, bit;

    hx_clock(1U);
    ok = hx_delay_us(2U);
    bit = (HX_REG(HX_IN) & HX_DATA_MASK) ? 1 : 0;
    hx_clock(0U);
    elapsed = (uint32_t)(TimerP_getTimeInUsecs() - start);
    HwiP_restore(key);

    if (elapsed > hx_max_high_us) hx_max_high_us = elapsed;
    if (ok != 0) return -1;
    if (elapsed >= 45U) return -3;
    if (hx_delay_us(2U) != 0) return -1;
    return bit;
}

/* A 通道、128 倍增益：24 個資料脈衝，加 1 個增益選擇脈衝。 */
static int hx_read_raw(int32_t *value)
{
    uint32_t bits = 0U, index;
    int result = hx_wait_ready();

    if (result != 0) return result;

    for (index = 0U; index < 24U; ++index) {
        result = hx_pulse();
        if (result < 0) return result;
        bits = (bits << 1) | (uint32_t)result;
    }

    result = hx_pulse();
    if (result < 0) return result;

    *value = (bits & 0x800000U)
        ? (int32_t)bits - 16777216
        : (int32_t)bits;
    return 0;
}

static int hx_initialize(void)
{
    uint32_t direction = HX_REG(HX_DIR);
    int32_t discard;
    int result;

    hx_initialized = 0U;
    hx_max_high_us = 0U;

    /* Linux 須先保留兩條線：資料輸入、時脈輸出且初始為低。 */
    if ((direction & HX_DATA_MASK) == 0U ||
        (direction & HX_CLK_MASK) != 0U) return -4;

    if (hx_delay_us(10U) != 0) return -1;

    hx_clock(0U);
    HX_REG(HX_PAD_DATA) = 0x00050007U;
    HX_REG(HX_PAD_CLK) = 0x00040007U;

    if ((HX_REG(HX_PAD_DATA) & 0x0027000FU) != 0x00050007U ||
        (HX_REG(HX_PAD_CLK) & 0x0027000FU) != 0x00040007U) return -5;

    /* 刻意拉高超過 60 微秒，重設 HX711。 */
    hx_clock(1U);
    result = hx_delay_us(100U);
    hx_clock(0U);
    if (result != 0) return result;

    /* 丟棄重設後第一筆資料。 */
    result = hx_read_raw(&discard);
    if (result != 0) return result;

    hx_sequence = 0U;
    hx_initialized = 1U;
    return 0;
}

static uint16_t bb_hx711_dispatch(char *buf, uint16_t len, uint16_t capacity)
{
    uintptr_t key;
    int result = 0, written = 0;
    int32_t raw = 0;
    uint64_t stamp;

    if (capacity == 0U || len < 3U ||
        memcmp(buf, "HX,", 3U) != 0) return 0U;

    key = HwiP_disable();
    if (hx_busy) {
        HwiP_restore(key);
        written = snprintf(buf, capacity, "HX,ERR,BUSY");
    } else {
        hx_busy = 1U;
        HwiP_restore(key);

        if (strcmp(buf, "HX,HELLO") == 0) {
            written = snprintf(buf, capacity,
                "HX,HELLO,HX711_R5F_V1,mcu2_0,P9.12,P9.15");

        } else if (strcmp(buf, "HX,INIT") == 0) {
            result = hx_initialize();
            written = snprintf(buf, capacity, "HX,INIT,%d", result);

        } else if (strcmp(buf, "HX,READ") == 0) {
            if (!hx_initialized) result = -6;
            else result = hx_read_raw(&raw);

            if (result == 0) {
                stamp = TimerP_getTimeInUsecs();
                ++hx_sequence;
                written = snprintf(buf, capacity,
                    "HX,RAW,%lu,%ld,%lu,%lu,%lu",
                    (unsigned long)hx_sequence, (long)raw,
                    (unsigned long)(uint32_t)(stamp >> 32),
                    (unsigned long)(uint32_t)stamp,
                    (unsigned long)hx_max_high_us);
            } else {
                hx_initialized = 0U;
                written = snprintf(buf, capacity, "HX,ERR,%d", result);
            }

        } else {
            written = snprintf(buf, capacity, "HX,ERR,COMMAND");
        }

        key = HwiP_disable();
        hx_busy = 0U;
        HwiP_restore(key);
    }

    if (written < 0 || capacity == 0U) return 0U;
    if ((uint32_t)written >= capacity) written = (int)capacity - 1;
    return (uint16_t)(written + 1);
}
#endif
