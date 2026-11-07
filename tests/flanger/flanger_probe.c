/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 * Independent exact public-API flanger checks; host only. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "fx.h"

static uint64_t checks, failures;
#define CHECK(x) do { checks++; if (!(x)) { if (failures < 12) fprintf(stderr,"FLANGERBOUND line=%d\n",__LINE__); failures++; } } while (0)
static int64_t down(int64_t n, int64_t d) { return n >= 0 ? n / d : -((-n + d - 1) / d); }
static int32_t clip32(int64_t n) { return n < INT32_MIN ? INT32_MIN : n > INT32_MAX ? INT32_MAX : (int32_t)n; }
static int16_t pcm(int64_t n) { n=down(n+((int64_t)1<<29),(int64_t)1<<30); return n < -32768 ? -32768 : n > 32767 ? 32767 : (int16_t)n; }
static uint32_t rounded(uint32_t us,uint32_t fs) { return (uint32_t)(((uint64_t)us*fs*65536+500000)/1000000); }
static uint32_t power(uint32_t n) { uint32_t p=1; while(p<n)p*=2;return p; }
static int32_t tap(const int32_t *line,uint32_t size,uint32_t n,uint32_t phase,uint32_t base,uint32_t depth)
{
    int64_t u=(int64_t)phase-2147483648LL;
    int64_t delay,frac;
    uint32_t whole;
    if(u<0)u=-u;
    u-=1073741824;
    delay=(int64_t)base+down((int64_t)depth*u,1073741824);
    if(delay<65536)delay=65536;
    whole=(uint32_t)(delay/65536);frac=delay%65536;
    return (int32_t)down((int64_t)line[(n-whole)%size]*(65536-frac)+(int64_t)line[(n-whole-1)%size]*frac,65536);
}
static fx_cfg_t cfg(uint32_t ch,uint32_t block,uint32_t base,uint32_t depth,uint32_t rate,uint32_t fb,uint32_t invert)
{
    fx_cfg_t c;memset(&c,0,sizeof c);c.on=FX_ON_FLANGER;c.channels=ch;c.block=block;
    c.flanger_base_us=base;c.flanger_depth_us=depth;c.flanger_rate_millihz=rate;c.flanger_fb_q15=fb;c.flanger_invert=invert;return c;
}
static uint32_t rng(uint32_t *s) { *s=*s*1664525u+1013904223u;return *s; }
static void raw(void)
{
    const uint32_t rates[]={8000,44100,48000,192000},blocks[]={32,64,128};
    const uint32_t values[][5]={{100,100,0,0,0},{100,100,2000,29491,1},{125,125,0,16384,0},
        {10000,10000,2000,29491,0},{10000,100,0,29491,1},{2000,1000,300,20000,0},{2000,1000,0,20000,1}};
    const uint32_t phases[]={0,0x3fffffff,0x40000000,0x7fffffff,0x80000000,0x80000001,0xbfffffff,0xc0000000,0xffffffff};
    size_t ri,bi,vi,pi;uint32_t ch;
    for(ri=0;ri<sizeof rates/sizeof rates[0];ri++)for(bi=0;bi<3;bi++)for(ch=1;ch<=2;ch++)
    for(vi=0;vi<sizeof values/sizeof values[0];vi++)for(pi=0;pi<sizeof phases/sizeof phases[0];pi++){
        fx_cfg_t c=cfg(ch,blocks[bi],values[vi][0],values[vi][1],values[vi][2],values[vi][3],values[vi][4]);
        uint32_t fs=rates[ri],base=rounded(c.flanger_base_us,fs),depth=rounded(c.flanger_depth_us,fs);
        uint32_t size=power((base+depth)/65536+2),phase=phases[pi],n=UINT32_MAX-127,seed=1,j;
        uint32_t step=(uint32_t)(((uint64_t)c.flanger_rate_millihz*4294967296ULL+(uint64_t)500*fs)/((uint64_t)1000*fs));
        fx_t *f=malloc(sizeof *f);void *mem=malloc(size*4u);int32_t *line=calloc(size,sizeof *line),*other=calloc(size,sizeof *other);
        CHECK(f&&mem&&line&&other);if(!f||!mem||!line||!other)exit(2);
        CHECK(fx_mem_bytes(&c,fs)==size*4u);CHECK(fx_init(f,fs,&c,mem,size*4u)==0);
        CHECK(f->flg_mask==size-1&&f->flg_base==base&&f->flg_depth==depth&&f->flg_inc==step&&f->flg_phase==0);
        for(j=0;j<size;j++){uint32_t w=rng(&seed);line[j]=j%4==0?INT32_MIN:j%4==1?INT32_MAX:(int32_t)(w&0x7fffffff)-(int32_t)(w>>31)*1073741824;f->flg_buf[j]=line[j];}
        /* A distinct silent chorus line makes the wrong-line control fail
         * on values rather than on a null pointer. Chorus itself is disabled. */
        f->cho_buf=other;f->flg_phase=phase;f->n=n;
        for(j=0;j<512;j++,n++,phase+=step){
            int64_t input=j%8==0?((int64_t)1<<52):j%8==1?-((int64_t)1<<52):j%8==2?-131073:j%8==3?131071:j<256?((int64_t)(int32_t)(rng(&seed)&0x7fffffff)-1073741824)*32768:0;
            int32_t left=tap(line,size,n,phase,base,depth),right=tap(line,size,n,phase+0x40000000u,base,depth);
            int64_t feedback=down((int64_t)left*c.flanger_fb_q15,32768);
            int32_t in=clip32(down(input+131072,262144)),write;
            int32_t gain=(ch==1?32768:23170)*8;int16_t out[2];
            if(c.flanger_invert)feedback=-feedback;
            write=clip32((int64_t)in+feedback);line[n%size]=write;
            f->bus[FX_FLANGER][0]=input;
            (void)fx_master(f,NULL,0,30,out,1);
            CHECK(out[0]==pcm((int64_t)left*gain));
            if(ch==2)CHECK(out[1]==pcm((int64_t)right*gain));
            CHECK(f->flg_buf[n%size]==write);
            CHECK(f->n==n+1&&f->flg_phase==phase+step&&f->bus[FX_FLANGER][0]==0);
        }
        CHECK(memcmp(line,f->flg_buf,size*4u)==0);
        CHECK(fx_init(f,fs,&c,mem,size*4u)==0);
        CHECK(f->n==0&&f->flg_phase==0&&f->send[0][FX_FLANGER].level==0);
        for(j=0;j<size;j++)CHECK(f->flg_buf[j]==0);
        free(other);free(line);free(mem);free(f);
    }
}
static void refusals(void)
{
    fx_cfg_t c=cfg(1,64,100,100,0,0,0);fx_t *f=malloc(sizeof *f),*saved=malloc(sizeof *saved);uint64_t mem[4096];uint32_t i;
    const uint32_t bad[][2]={{0,99},{0,10001},{1,99},{1,10001},{2,2001},{3,29492},{4,2},
        {0,UINT32_MAX},{1,UINT32_MAX},{2,UINT32_MAX},{3,UINT32_MAX},{4,UINT32_MAX}};
    CHECK(f&&saved);if(!f||!saved)exit(2);
    for(i=0;i<sizeof bad/sizeof bad[0];i++){
        fx_cfg_t b=c;uint32_t *p=bad[i][0]==0?&b.flanger_base_us:bad[i][0]==1?&b.flanger_depth_us:bad[i][0]==2?&b.flanger_rate_millihz:bad[i][0]==3?&b.flanger_fb_q15:&b.flanger_invert;
        *p=bad[i][1];memset(f,0xa5,sizeof *f);memcpy(saved,f,sizeof *f);memset(mem,0xa5,sizeof mem);
        CHECK(fx_mem_bytes(&b,48000)==0&&fx_init(f,48000,&b,mem,sizeof mem)==-1);
        CHECK(memcmp(f,saved,sizeof *f)==0&&mem[0]==UINT64_C(0xa5a5a5a5a5a5a5a5));
        b.on=0;CHECK(fx_init(f,48000,&b,NULL,0)==0);
    }
    c.flanger_depth_us=101;CHECK(fx_init(f,48000,&c,mem,sizeof mem)==-1);c.flanger_depth_us=100;
    c.channels=4;CHECK(fx_init(f,48000,&c,mem,sizeof mem)==-1);c.channels=16;CHECK(fx_init(f,48000,&c,mem,sizeof mem)==-1);c.channels=1;
    CHECK(fx_init(f,7999,&c,mem,sizeof mem)==-1);CHECK(fx_init(f,192001,&c,mem,sizeof mem)==-1);
    CHECK(fx_init(f,48000,&c,NULL,sizeof mem)==-1);CHECK(fx_init(f,48000,&c,(char*)mem+1,sizeof mem-1)==-1);
    CHECK(fx_init(f,48000,&c,mem,fx_mem_bytes(&c,48000)-1)==-1);
    free(f);free(saved);
}
static void partitions(void)
{
    const uint32_t rates[]={8000,44100,48000,192000};size_t ri;uint32_t k,ch,seed=55;
    for(ri=0;ri<4;ri++)for(k=32;k<=128;k*=2)for(ch=1;ch<=2;ch++){
        fx_cfg_t c=cfg(ch,k,100,100,2000,29491,1);size_t bytes=fx_mem_bytes(&c,rates[ri]);
        fx_t *a=malloc(sizeof *a),*b=malloc(sizeof *b);void *ma=malloc(bytes),*mb=malloc(bytes);
        int32_t *other=calloc(bytes,1);
        int32_t samples[128];int16_t oa[256],ob[256];uint32_t block,t;
        CHECK(a&&b&&ma&&mb&&other);if(!a||!b||!ma||!mb||!other)exit(2);
        CHECK(!fx_init(a,rates[ri],&c,ma,bytes)&&!fx_init(b,rates[ri],&c,mb,bytes));
        a->cho_buf=b->cho_buf=other; /* distinct silent line for the wrong-line control */
        a->n=b->n=UINT32_MAX-511;
        for(block=0;block<32;block++){
            uint32_t level=block%3==0?32768:block%3==1?12345:0,done=0;
            for(t=0;t<k;t++)samples[t]=block<16?(int32_t)(rng(&seed)&0x7fffffff)-1073741824:0;
            fx_set_send(a,0,FX_FLANGER,level);fx_set_send(b,0,FX_FLANGER,level);
            fx_voice_sends(a,0,samples,k);(void)fx_master(a,NULL,0,30,oa,k);
            while(done<k){uint32_t n=1+rng(&seed)%19;if(n>k-done)n=k-done;
                fx_voice_sends(b,0,samples+done,n);(void)fx_master(b,NULL,0,30,ob+done*ch,n);done+=n;}
            CHECK(memcmp(oa,ob,k*ch*sizeof *oa)==0);CHECK(memcmp(ma,mb,bytes)==0);
            CHECK(a->n==b->n&&a->flg_phase==b->flg_phase&&a->send[0][FX_FLANGER].gp==b->send[0][FX_FLANGER].gp&&a->send[0][FX_FLANGER].gt==b->send[0][FX_FLANGER].gt);
        }
        free(other);free(a);free(b);free(ma);free(mb);
    }
}
int main(void)
{
    CHECK(FX_CHORUS==0&&FX_DELAY==1&&FX_REVERB==2&&FX_DUCK==3&&FX_FLANGER==4&&FX_BUSES==5);
    raw();refusals();partitions();
    printf("FLANGERSIZE fx_t=%zu cfg=%zu buses=%u\n",sizeof(fx_t),sizeof(fx_cfg_t),(unsigned)FX_BUSES);
    printf("FLANGERBOUND checks=%llu failures=%llu\n",(unsigned long long)checks,(unsigned long long)failures);
    return failures?1:0;
}
