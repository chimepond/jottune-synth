/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 * Host-only independent real field/FX model, never included by the library. */
#ifndef FIELD_REF_H
#define FIELD_REF_H
#include "../space/field.h"
typedef struct fieldr fieldr_t;
/* Configuration is admitted by disposable native init/attach checks only.
 * Signals, taps, gains, histories and FX state are independently calculated.
 * Caller keeps cfg->table valid and immutable until fieldr_free. */
fieldr_t *fieldr_new(const sp_field_cfg_t *cfg, const fx_cfg_t *fxcfg);
void fieldr_free(fieldr_t *r);
void fieldr_set_pos(fieldr_t *r,uint32_t voice,int32_t x,int32_t y,int32_t z);
void fieldr_move(fieldr_t *r,uint32_t voice,int32_t x,int32_t y,int32_t z,uint32_t end);
void fieldr_set_yaw(fieldr_t *r,int32_t mdeg);
void fieldr_set_send(fieldr_t *r,uint32_t voice,uint32_t bus,uint32_t level);
void fieldr_set_crush(fieldr_t *r,uint32_t voice,uint32_t bits,uint32_t div);
/* One frame of raw pre-crusher/pre-pinna Q2.30 samples, voice order 0..count-1.
 * Out has the configured 2/4/16 channels, in unclipped/unrounded int16 units.
 * The reference owns its counter and advances FX once. No source samples or
 * hidden states are borrowed from a production field or effects instance. */
void fieldr_step(fieldr_t *r,const int32_t *raw_q30,uint32_t count,double *out);
#endif
