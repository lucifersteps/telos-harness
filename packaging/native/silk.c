/* Telos bounded SILK file adapter. Copyright (c) 2026 Telos contributors.
 * SPDX-License-Identifier: MIT. Codec source retains its own Skype license. */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include "SKP_Silk_SDK_API.h"
#define RATE 16000
#define MAX_SAMPLES (120 * RATE)

static int decode(FILE *in, FILE *out) {
    char header[9];
    int first = fgetc(in);
    if (first != 2 && first != EOF) ungetc(first, in);
    if (fread(header, 1, 9, in) != 9 || memcmp(header, "#!SILK_V3", 9)) return 1;
    SKP_int32 size = 0;
    if (SKP_Silk_SDK_Get_Decoder_Size(&size) || size <= 0) return 1;
    void *state = calloc(1, (size_t)size);
    if (!state) return 1;
    int result = 1, total = 0;
    if (SKP_Silk_SDK_InitDecoder(state)) goto done;
    SKP_SILK_SDK_DecControlStruct control = {0};
    control.API_sampleRate = RATE;
    unsigned char payload[32767];
    for (;;) {
        int lo = fgetc(in), hi;
        if (lo == EOF) { result = ferror(in) || total == 0; break; }
        hi = fgetc(in);
        if (hi == EOF) break;
        int length = (int16_t)(lo | (hi << 8));
        if (length == -1) { result = fgetc(in) != EOF || total == 0; break; }
        if (length <= 0 || fread(payload, 1, (size_t)length, in) != (size_t)length) break;
        int frames = 0;
        do {
            SKP_int16 pcm[960], samples = 960;
            if (++frames > 5 || SKP_Silk_SDK_Decode(state, &control, 0, payload, length, pcm, &samples)) goto done;
            if (samples <= 0 || samples > 960 || total + samples > MAX_SAMPLES) goto done;
            if (fwrite(pcm, sizeof(*pcm), (size_t)samples, out) != (size_t)samples) goto done;
            total += samples;
        } while (control.moreInternalDecoderFrames);
    }
done:
    free(state);
    return result;
}

/* Encoder is used for synthetic compatibility fixtures; no microphone input. */
static int encode(FILE *in, FILE *out) {
    SKP_int32 size = 0;
    if (SKP_Silk_SDK_Get_Encoder_Size(&size) || size <= 0) return 1;
    void *state = calloc(1, (size_t)size);
    if (!state) return 1;
    SKP_SILK_SDK_EncControlStruct control = {0};
    int result = 1, total = 0;
    if (SKP_Silk_SDK_InitEncoder(state, &control)) goto done;
    control.API_sampleRate = RATE; control.maxInternalSampleRate = RATE;
    control.packetSize = 320; control.bitRate = 24000; control.complexity = 2;
    if (fwrite("\002#!SILK_V3", 1, 10, out) != 10) goto done;
    for (;;) {
        SKP_int16 pcm[320] = {0}; unsigned char payload[4096]; SKP_int16 length = sizeof(payload);
        size_t count = fread(pcm, sizeof(*pcm), 320, in);
        if (!count) { result = ferror(in) || total == 0; break; }
        total += 320;
        if (total > MAX_SAMPLES || SKP_Silk_SDK_Encode(state, &control, pcm, 320, payload, &length)) goto done;
        if (length <= 0 || fputc(length & 255, out) == EOF || fputc(length >> 8, out) == EOF ||
            fwrite(payload, 1, (size_t)length, out) != (size_t)length) goto done;
    }
done:
    free(state);
    return result;
}
int main(int argc, char **argv) {
    if (argc != 4 || (strcmp(argv[1], "decode") && strcmp(argv[1], "encode"))) return 2;
    FILE *in = fopen(argv[2], "rb"); if (!in) return 2;
    FILE *out = fopen(argv[3], "wb"); if (!out) { fclose(in); return 2; }
    int result = !strcmp(argv[1], "decode") ? decode(in, out) : encode(in, out);
    if (fclose(out)) result = 1;
    fclose(in);
    return result;
}
