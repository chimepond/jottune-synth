/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0
 * P275 focused API and state controls. Host only, no WAV. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <math.h>
#include "field_ref.h"
#include "../space/field.h"

static unsigned long checks, failures;
static uint64_t digest = UINT64_C(14695981039346656037);
static void need(int yes, const char *why) { checks++; if (!yes && failures++ < 20) printf("FAIL %s\n",why); }
static void pcm_hash(const int16_t *x, size_t n) { size_t i; for(i=0;i<n;i++) { unsigned v=(uint16_t)x[i]; digest^=v&255u;digest*=UINT64_C(1099511628211);digest^=v>>8;digest*=UINT64_C(1099511628211); } }
static fx_cfg_t config(unsigned on, unsigned ch, unsigned k) {
    fx_cfg_t c;memset(&c,0,sizeof c);c.on=on;c.channels=ch;c.block=k;
    c.dc_millihz=17000;c.chorus_base_us=7000;c.chorus_depth_us=1000;c.chorus_rate_millihz=900;
    c.flanger_base_us=1000;c.flanger_depth_us=800;c.flanger_rate_millihz=700;c.flanger_fb_q15=7000;c.flanger_invert=1;
    c.delay_us=50000;c.delay_fb_q15=6000;c.delay_lp_hz=5000;c.delay_hp_hz=37;
    c.reverb_room=300;c.reverb_damp=200;c.limiter_db10=10;c.limiter_release_ms=50;
    c.compressor_db10=100;c.compressor_ratio_tenths=40;c.compressor_attack_ms=3;c.compressor_release_ms=60;c.compressor_makeup_db10=30;
    c.gain_db10=-30;c.dither_seed=7;return c;
}
static sp_field_cfg_t fieldconfig(unsigned fs, unsigned order, int route, int pinna, unsigned k, const int16_t *h, unsigned shift) {
    sp_field_cfg_t c;memset(&c,0,sizeof c);c.fs=fs;c.order=order;c.route=route;c.pinna=pinna;c.block=k;c.near_mm=100;c.table=h;c.table_shift=shift;c.panlaw_q15=32768;
    c.pinna_d[0]=1000;c.pinna_d[1]=0;c.pinna_d[2]=250;c.pinna_d[3]=750;c.pinna_d[4]=1000;return c;
}
static void *initfx(fx_t *f, fx_cfg_t *c, unsigned fs, int field) {
    size_t n=field?fx_field_mem_bytes(c,fs):fx_mem_bytes(c,fs);void *p=calloc(n?n:8,1);
    if(!p)exit(2);
    need((field?fx_field_init(f,fs,c,p,n):fx_init(f,fs,c,p,n))==0,"fx init");return p;
}
static void table_fill(int16_t *h) { unsigned c,j;for(c=0;c<16;c++)for(j=0;j<256;j++)h[c*256+j]=(int16_t)(j<4?((c&1u)?-1:1)*(int)(400/(j+1)):0); }
static void attachment(unsigned fs) {
    sp_field_t *f=calloc(1,sizeof *f);fx_t *x=calloc(1,sizeof *x),*old=calloc(1,sizeof *old);int16_t h[4096]={0};
    unsigned order,shift,ch,bit;void *p;sp_field_cfg_t fc;fx_cfg_t c;
    if(!f||!x||!old)exit(2);
    for(ch=1;ch<=16;ch*=2) {
        c=config(FX_ON_REVERB,ch,64);p=calloc(500000,1);
        if(!p)exit(2);
        need((fx_field_init(x,fs,&c,p,500000)==0)==(ch==2||ch==4||ch==16),"field channel admission");
        need((fx_init(old,fs,&c,p,500000)==0)==(ch<=2),"legacy bus channel refusal");free(p);
    }
    for(ch=4;ch<=16;ch*=4)for(bit=0;bit<4;bit++) {
        static const unsigned bad[]={FX_ON_COMPRESSOR,FX_ON_LIMITER,FX_ON_SOFTCLIP,FX_ON_DITHER};
        c=config(bad[bit],ch,64);need(fx_field_mem_bytes(&c,fs)==0,"forbidden raw memory");need(fx_field_init(x,fs,&c,NULL,0)==-1,"forbidden raw stage");
    }
    for(order=1;order<=3;order+=2)for(shift=1;shift<=30;shift+=(shift==1?13:shift==14?1:15)) {
        uint64_t cap=(UINT64_C(1)<<(shift+6))-1, left=cap;unsigned i=0,nsh=order==1?4:16;
        memset(h,0,sizeof h);
        while(left&&i<nsh*256) { unsigned v=left>32768?32768:(unsigned)left;h[i++]=(int16_t)(-(int)v);left-=v; }
        fc=fieldconfig(fs,order,SP_FIELD_BINAURAL,0,64,h,shift);c=config(0,2,64);p=initfx(x,&c,fs,1);
        need(sp_field_init(f,&fc)==0,"table-bound field init");need(sp_field_attach_fx(f,x)==0,"table H at/below cap");
        if(!left&&i<nsh*256) { h[i]=-1;need(sp_field_attach_fx(f,old)==-1,"wrong mode doesn't publish");need(f->geo.fx==x,"failed attach retains pointer");
            need(sp_field_attach_fx(f,NULL)==0,"detach at zero");need(sp_field_attach_fx(f,x)==-1,"cap plus one refuses");need(f->geo.fx==NULL,"failed cap no publication");h[i]=0; }
        need(sp_field_attach_fx(f,x)==0,"reattach admitted");
        x->fs++;need(sp_field_attach_fx(f,x)==-1,"rate mismatch");x->fs--;
        x->k=32;need(sp_field_attach_fx(f,x)==-1,"block mismatch");x->k=64;
        x->ch=4;need(sp_field_attach_fx(f,x)==-1,"channel mismatch");x->ch=2;
        x->n=1;need(sp_field_attach_fx(f,x)==-1,"fx advanced");x->n=0;
        x->open=1;need(sp_field_attach_fx(f,x)==-1,"open collecting");x->open=2;need(sp_field_attach_fx(f,x)==-1,"open returns");x->open=0;
        f->geo.n=1;need(sp_field_attach_fx(f,NULL)==-1,"late detach refused");need(f->geo.fx==x,"late detach preserves");f->geo.n=0;
        need(sp_field_init(f,&fc)==0&&f->geo.fx==NULL,"init clears old attachment");free(p);
    }
    fc=fieldconfig(fs,1,SP_FIELD_PANLAW,0,64,h,14);c=config(0,2,64);p=initfx(x,&c,fs,1);
    need(sp_field_init(f,&fc)==0&&sp_field_attach_fx(f,x)==-1,"panlaw refuses all FX");free(p);free(old);free(x);free(f);
}
#define N 4099u
static void bypass(unsigned fs) {
    int16_t h[4096];sp_field_t *a=calloc(1,sizeof *a),*b=calloc(1,sizeof *b);fx_t *fx=calloc(1,sizeof *fx);
    int32_t *src=malloc(N*3*sizeof *src);int16_t *x=malloc(N*16*sizeof *x),*y=malloc(N*16*sizeof *y);unsigned r,p,k,g,n,c;uint32_t seed=7;
    if(!a||!b||!fx||!src||!x||!y)exit(2);
    table_fill(h);
    for(n=0;n<N*3;n++){ seed=seed*1664525u+1013904223u;src[n]=(int32_t)seed/8; }
    for(r=0;r<4;r++)for(p=0;p<4;p++)for(k=32;k<=128;k*=2)for(g=0;g<2;g++) {
        int route=r==0?SP_FIELD_AMB1:r==1?SP_FIELD_AMB3:SP_FIELD_BINAURAL;unsigned order=r==2?1:3;
        sp_field_cfg_t fc=fieldconfig(fs,order,route,(int)p,k,h,14);fx_cfg_t xc=config(g?FX_ON_GAIN:0,r==0?4:r==1?16:2,k);void *mem;unsigned ca=0,cb=0;
        xc.gain_db10=0;mem=initfx(fx,&xc,fs,1);need(sp_field_init(a,&fc)==0&&sp_field_init(b,&fc)==0,"bypass field init");need(sp_field_attach_fx(b,fx)==0,"bypass attach");
        for(c=0;c<3;c++){sp_stereo_set_pos(&a->geo,c,(int)c*500-500,700,(int)c*50);sp_stereo_set_pos(&b->geo,c,(int)c*500-500,700,(int)c*50);}
        for(n=0;n<N;) { unsigned len=n<1031?1031-n:N-n;
        if(n==1031){sp_field_set_yaw(a,-72500);sp_field_set_yaw(b,-72500);}
            ca+=sp_field_render_q30(a,src+n*3,3,3,x+n*a->ch,len);
            for(c=0;c<len;c++)cb+=sp_field_render_q30(b,src+(n+c)*3,3,3,y+(n+c)*b->ch,1);
            n+=len;
        }
        need(ca==cb,"bypass clip counts");need(memcmp(x,y,N*a->ch*sizeof *x)==0,"on0/gain0 exact bypass and chunks");need(fx->n==N&&b->geo.n==N&&fx->open==0&&fx->piece_len==0,"bypass counter closure");pcm_hash(y,N*b->ch);free(mem);
    }
    free(y);free(x);free(src);free(fx);free(b);free(a);
}
static void fx_equal(const fx_t *a,const fx_t *b,const void *ma,const void *mb,size_t bytes) {
    fx_t x=*a,y=*b;unsigned c,j;
    x.field_mode=y.field_mode=0;x.lim_ring=y.lim_ring=NULL;x.cho_buf=y.cho_buf=NULL;x.flg_buf=y.flg_buf=NULL;x.dly.buf=y.dly.buf=NULL;
    for(c=0;c<2;c++)for(j=0;j<8;j++)x.comb[c][j].buf=y.comb[c][j].buf=NULL;
    for(c=0;c<2;c++)for(j=0;j<4;j++)x.allp[c][j].buf=y.allp[c][j].buf=NULL;
    need(memcmp(&x,&y,sizeof x)==0,"split complete FX state");need(memcmp(ma,mb,bytes)==0,"split independent caller memories");
}
static void split(unsigned fs,unsigned k) {
    static const unsigned modes[]={0,FX_ON_CHORUS,FX_ON_FLANGER,FX_ON_DELAY,FX_ON_DELAY|FX_ON_PINGPONG,FX_ON_REVERB,FX_ON_COMPRESSOR,FX_ON_ALL};
    unsigned m,n,t,c,b;fx_t *a=malloc(sizeof *a),*d=malloc(sizeof *d);
    if(!a||!d)exit(2);
    for(m=0;m<sizeof modes/sizeof modes[0];m++) {
        fx_cfg_t cfg=config(modes[m],2,k);void *ma=initfx(a,&cfg,fs,0),*md=initfx(d,&cfg,fs,1);size_t bytes=fx_mem_bytes(&cfg,fs);
        need(bytes==fx_field_mem_bytes(&cfg,fs),"stereo split memory equal");
        for(b=0;b<FX_BUSES;b++){fx_set_send(a,0,b,7000+b*3000);fx_set_send(d,0,b,7000+b*3000);}fx_set_crush(a,0,10,13);fx_set_crush(d,0,10,13);
        for(n=0;n<8192;) {
            int32_t xa[128],xd[128],wet[4][2][128];int64_t acc[2][128],post[2][128];int16_t oa[256],od[256];unsigned len=k-(n%k),ca,cd;
            if(len>8192-n)len=8192-n;
            if(len>31)len=31;
            if(n==0||n==4096)for(b=0;b<FX_BUSES;b++){fx_set_send(a,0,b,n?0:7000+b*3000);fx_set_send(d,0,b,n?0:7000+b*3000);}
            for(t=0;t<len;t++)xa[t]=xd[t]=(n+t<3000)?(int32_t)((n+t)%79)*1000000-39000000:0;
            fx_voice_process(a,0,xa,len);fx_voice_process(d,0,xd,len);need(memcmp(xa,xd,len*sizeof *xa)==0,"split insert exact");
            need(d->n==n&&d->open==1&&d->piece_len==len,"collecting piece identity");
            for(t=0;t<len;t++)for(c=0;c<2;c++)acc[c][t]=(int64_t)xa[t]*10000;
            ca=fx_master(a,&acc[0][0],128,45,oa,len);fx_field_returns(d,&wet[0][0][0],256,128,len);
            need(d->n==n&&d->open==2&&d->piece_len==len,"returns do not advance");
            for(t=0;t<len;t++)for(c=0;c<2;c++){post[c][t]=acc[c][t];for(b=0;b<4;b++)post[c][t]+=(int64_t)wet[b][c][t]*(23170*8);}
            cd=fx_master_post(d,&post[0][0],128,od,len);need(ca==cd&&memcmp(oa,od,2*len*sizeof *oa)==0,"split PCM exact legacy order");
            need(d->n==n+len&&d->open==0&&d->piece_len==0,"post closes once");fx_equal(a,d,ma,md,bytes);pcm_hash(od,len*2);n+=len;
        }
        free(md);free(ma);
    }
    free(d);free(a);
}

static void field_equal(const sp_field_t *a,const sp_field_t *b) {
    sp_field_t *x=malloc(sizeof *x),*y=malloc(sizeof *y);
    if(!x||!y)exit(2);
    *x=*a;*y=*b;
    x->geo.fx=y->geo.fx=NULL;x->table=y->table=NULL;
    memset(x->geo.buf,0,sizeof x->geo.buf);memset(y->geo.buf,0,sizeof y->geo.buf);
    memset(x->geo.acc,0,sizeof x->geo.acc);memset(y->geo.acc,0,sizeof y->geo.acc);
    memset(x->buf,0,sizeof x->buf);memset(y->buf,0,sizeof y->buf);memset(x->pbuf,0,sizeof x->pbuf);memset(y->pbuf,0,sizeof y->pbuf);
    memset(x->wetbuf,0,sizeof x->wetbuf);memset(y->wetbuf,0,sizeof y->wetbuf);memset(x->acc,0,sizeof x->acc);memset(y->acc,0,sizeof y->acc);
    memset(x->fld,0,sizeof x->fld);memset(y->fld,0,sizeof y->fld);memset(x->eo,0,sizeof x->eo);memset(y->eo,0,sizeof y->eo);
    need(memcmp(x,y,sizeof *x)==0,"field full logical state across chunks");free(y);free(x);
}
static void events(sp_field_t *f,fx_t *x,unsigned n) {
    unsigned i,b;
    if(n==0)for(i=0;i<3;i++){
        sp_stereo_set_pos(&f->geo,i,(int)i*500-500,900,(int)i*80);
        fx_set_crush(x,i,11+i,13+i);
        for(b=0;b<FX_BUSES;b++)fx_set_send(x,i,b,900+i*1000+b*700);
    }
    if(n==127){sp_field_set_yaw(f,37500);fx_set_send(x,0,FX_DUCK,0);fx_set_send(x,1,FX_DELAY,10000);fx_set_crush(x,2,8,7);}
    if(n==1031){sp_field_set_yaw(f,-157300);sp_stereo_move(&f->geo,0,900,-1000,300,3107);}
    if(n==2403)for(i=0;i<3;i++)for(b=0;b<FX_BUSES;b++)fx_set_send(x,i,b,0);
}
static void partitions(unsigned fs,unsigned k) {
    static const unsigned ev[]={0,127,1031,2403,4099};
    int16_t h[4096];int32_t *src=malloc(N*3*sizeof *src);int16_t *oa=malloc(N*16*sizeof *oa),*ob=malloc(N*16*sizeof *ob);
    sp_field_t *a=calloc(1,sizeof *a),*b=calloc(1,sizeof *b);fx_t *xa=malloc(sizeof *xa),*xb=malloc(sizeof *xb);
    unsigned r,p,mode,n,t,e;uint32_t seed=3;
    if(!src||!oa||!ob||!a||!b||!xa||!xb)exit(2);
    table_fill(h);
    for(t=0;t<N;t++)for(r=0;r<3;r++){seed=seed*1664525u+1013904223u;src[t*3+r]=t<1801?(int32_t)seed/32:0;}
    for(r=0;r<4;r++)for(p=0;p<4;p++)for(mode=0;mode<3;mode++) {
        unsigned ch=r==0?4:r==1?16:2, on=FX_ON_BUSES|FX_ON_GAIN|FX_ON_DC|FX_ON_CRUSH;
        sp_field_cfg_t fc=fieldconfig(fs,r==2?1:3,r==0?SP_FIELD_AMB1:r==1?SP_FIELD_AMB3:SP_FIELD_BINAURAL,(int)p,k,h,14);
        fx_cfg_t xc;void *ma,*mb;unsigned ca=0,cb=0;
        if(r>=2)on|=FX_ON_COMPRESSOR|FX_ON_LIMITER|FX_ON_DITHER|FX_ON_SOFTCLIP;
        xc=config(on,ch,k);ma=initfx(xa,&xc,fs,1);mb=initfx(xb,&xc,fs,1);
        memset(a,0,sizeof *a);memset(b,0,sizeof *b);need(sp_field_init(a,&fc)==0&&sp_field_init(b,&fc)==0,"partition field init");need(sp_field_attach_fx(a,xa)==0&&sp_field_attach_fx(b,xb)==0,"partition attach");
        for(e=0;e<4;e++){
            events(a,xa,ev[e]);events(b,xb,ev[e]);ca+=sp_field_render_q30(a,src+ev[e]*3,3,3,oa+ev[e]*ch,ev[e+1]-ev[e]);
            for(n=ev[e];n<ev[e+1];){unsigned len=mode==0?1:mode==1?63:1+(seed=seed*1664525u+1013904223u)%200;
                if(len>ev[e+1]-n)len=ev[e+1]-n;
                cb+=sp_field_render_q30(b,src+n*3,3,3,ob+n*ch,len);n+=len;}
            need(ca==cb&&memcmp(oa,ob,ev[e+1]*ch*sizeof *oa)==0,"field PCM partition/events/returns");field_equal(a,b);fx_equal(xa,xb,ma,mb,fx_field_mem_bytes(&xc,fs));
        }
        pcm_hash(ob,N*ch);free(mb);free(ma);
    }
    free(xb);free(xa);free(b);free(a);free(ob);free(oa);free(src);
}

static void idle(unsigned fs,int pinna) {
    sp_field_t *a=calloc(1,sizeof *a),*b=calloc(1,sizeof *b);fx_t *xa=malloc(sizeof *xa),*xb=malloc(sizeof *xb);
    fm_voice_t va,vb,saved;fm_patch_t patch;int16_t h[4096],oa[128*4],ob[128*4];int32_t impulse=123456789;unsigned t;
    sp_field_cfg_t fc;fx_cfg_t xc=config(FX_ON_CRUSH|FX_ON_DELAY,4,64);void *ma,*mb;
    if(!a||!b||!xa||!xb)exit(2);
    memset(&patch,0,sizeof patch);memset(&va,0,sizeof va);memset(&vb,0,sizeof vb);table_fill(h);
    fm_voice_init(&va,&patch,fs);fm_voice_init(&vb,&patch,fs);saved=va;
    fc=fieldconfig(fs,1,SP_FIELD_AMB1,pinna,64,h,14);ma=initfx(xa,&xc,fs,1);mb=initfx(xb,&xc,fs,1);
    need(sp_field_init(a,&fc)==0&&sp_field_init(b,&fc)==0,"idle field init");need(sp_field_attach_fx(a,xa)==0&&sp_field_attach_fx(b,xb)==0,"idle attach");
    fx_set_crush(xa,0,12,13);fx_set_crush(xb,0,12,13);fx_set_send(xa,0,FX_DELAY,32768);fx_set_send(xb,0,FX_DELAY,32768);
    sp_field_render_q30(a,&impulse,1,1,oa,1);sp_field_render_q30(b,&impulse,1,1,ob,1);
    sp_field_render(a,&va,1,oa,31);for(t=0;t<31;t++)sp_field_render(b,&vb,1,ob+t*4,1);
    need(memcmp(oa,ob,31*4*sizeof *oa)==0,"silent held-output partition PCM");
    need(memcmp(&va,&saved,sizeof va)==0&&memcmp(&vb,&saved,sizeof vb)==0,"idle engines untouched");
    need(oa[0]!=0,"idle held output is not discarded");field_equal(a,b);fx_equal(xa,xb,ma,mb,fx_field_mem_bytes(&xc,fs));
    free(mb);free(ma);free(xb);free(xa);free(b);free(a);
}

static void model_events(sp_field_t *f,fx_t *x,fieldr_t *r,unsigned n) {
    unsigned i,b;
    if(n==0)for(i=0;i<3;i++){
        sp_stereo_set_pos(&f->geo,i,(int)i*500-500,900,(int)i*80);fieldr_set_pos(r,i,(int)i*500-500,900,(int)i*80);
        fx_set_crush(x,i,15+i,3+i);fieldr_set_crush(r,i,15+i,3+i);
        for(b=0;b<FX_BUSES;b++){fx_set_send(x,i,b,5000+i*1000+b*700);fieldr_set_send(r,i,b,5000+i*1000+b*700);}
    }
    if(n==127){sp_field_set_yaw(f,37500);fieldr_set_yaw(r,37500);fx_set_send(x,0,FX_DUCK,0);fieldr_set_send(r,0,FX_DUCK,0);}
    if(n==1031){sp_field_set_yaw(f,-157300);fieldr_set_yaw(r,-157300);sp_stereo_move(&f->geo,0,900,-1000,300,3107);fieldr_move(r,0,900,-1000,300,3107);}
    if(n==2403)for(i=0;i<3;i++)for(b=0;b<FX_BUSES;b++){fx_set_send(x,i,b,0);fieldr_set_send(r,i,b,0);}
}
static double clip_real(double x) { return x>32767?32767:x< -32768?-32768:x; }
static void model(unsigned fs) {
    static const unsigned modes[]={FX_ON_CHORUS,FX_ON_FLANGER,FX_ON_DELAY|FX_ON_PINGPONG,FX_ON_REVERB,FX_ON_BUSES,FX_ON_BUSES|FX_ON_CRUSH|FX_ON_GAIN|FX_ON_DC};
    unsigned route,m;int16_t h[4096];table_fill(h);
    for(route=0;route<4;route++)for(m=0;m<6;m++) {
        unsigned ch=route==0?4:route==1?16:2,on=modes[m],pinna=(m+route)%4,k=32u<<(m%3),n,i,c,total=(route==2&&m==5)?fs*2:6000;
        sp_field_cfg_t fc=fieldconfig(fs,route==2?1:3,route==0?SP_FIELD_AMB1:route==1?SP_FIELD_AMB3:SP_FIELD_BINAURAL,(int)pinna,k,h,14);
        fx_cfg_t xc;sp_field_t *f=calloc(1,sizeof *f);fx_t *fx=malloc(sizeof *fx);fieldr_t *r;void *mem;
        double sig=0,err=0,qerr=0,peak=0,loss;uint64_t aend=0,rend=0,lo=0,hi=0,spurious=0,band=0;unsigned clips=0;
        if(!f||!fx)exit(2);
        if(route>=2&&m==5)on|=FX_ON_COMPRESSOR|FX_ON_LIMITER;
        xc=config(on,ch,k);mem=initfx(fx,&xc,fs,1);need(sp_field_init(f,&fc)==0&&sp_field_attach_fx(f,fx)==0,"model production admission");r=fieldr_new(&fc,&xc);need(r!=NULL,"independent model admission");
        if(!r)exit(2);
        for(n=0;n<total;n++) {
            int32_t input[3];int16_t out[16];double ref[16];
            for(i=0;i<3;i++)input[i]=n<1801?(int32_t)(sin((n+19*i)*(0.031+0.009*i))*7000000):0;
            model_events(f,fx,r,n);clips+=sp_field_render_q30(f,input,3,3,out,1);fieldr_step(r,input,3,ref);
            for(c=0;c<ch;c++) {
                uint64_t ix=(uint64_t)n*ch+c+1;double v=ref[c],e=out[c]-v,q=clip_real(floor(v+0.5))-v,d=fabs(out[c]-clip_real(v));
                sig+=v*v;err+=e*e;qerr+=q*q;
                if(d>peak)peak=d;
                if(out[c])aend=ix;
                if(clip_real(floor(v+0.5)))rend=ix;
                if(fabs(v)>=0.5+1.0/1024){lo=ix;spurious=band=0;}else if(fabs(v)>=0.5-1.0/1024)band++;else if(out[c])spurious++;
                if(fabs(v)>=0.5-1.0/1024)hi=ix;
            }pcm_hash(out,ch);
        }
        loss=(err>0&&qerr>0)?10*log10(err/qerr):0;
        printf("FIELD_FX_REF fs=%u route=%u pinna=%u on=%x frames=%u snr=%.6f loss=%.6f peak=%.6f clipped=%u actual=%llu model=%llu lower=%llu upper=%llu band=%llu spurious=%llu\n",fs,route,pinna,on,total,err>0?10*log10(sig/err):999.0,loss,peak,clips,(unsigned long long)aend,(unsigned long long)rend,(unsigned long long)lo,(unsigned long long)hi,(unsigned long long)band,(unsigned long long)spurious);
        need(clips==0,"D14 nonclipping fixture");need(loss<=0.10&&peak<=1.0,"independent field FX D14");need(aend>=lo&&aend<=hi&&spurious==0,"full D27 lower upper post-bound samples");
        if(total>6000)need(hi<(uint64_t)(total-256)*ch,"complete dry-off return tail drained");
        fieldr_free(r);free(mem);free(fx);free(f);
    }
}
static const int32_t expected[8][16]={
    {8388608,4194304,0,7264748,6291456,0,-4194304,0,3632374,6631777,0,-2568476,0,-4448731,0,0},
    {8388608,-4194304,0,7264748,-6291456,0,-4194304,0,3632374,-6631777,0,2568476,0,-4448731,0,0},
    {8388608,7264748,0,-4194304,-6291456,0,-4194304,0,-3632374,0,0,-4448731,0,2568476,0,6631777},
    {8388608,7264748,0,4194304,6291456,0,-4194304,0,-3632374,0,0,-4448731,0,-2568476,0,-6631777},
    {8388608,2097152,0,3632374,1572864,0,-1048576,0,908093,828972,0,-321060,0,-556091,0,0},
    {8388608,-2097152,0,3632374,-1572864,0,-1048576,0,908093,-828972,0,321060,0,-556091,0,0},
    {8388608,3632374,0,-2097152,-1572864,0,-1048576,0,-908093,0,0,-556091,0,321060,0,828972},
    {8388608,3632374,0,2097152,1572864,0,-1048576,0,-908093,0,0,-556091,0,-321060,0,-828972}
};

static void targets(unsigned fs) {
    sp_field_t *f=calloc(1,sizeof *f);fx_t *fx=malloc(sizeof *fx);int16_t out[128*16],h[4096]={0};unsigned near,yaw,order,b,c;
    if(!f||!fx)exit(2);
    for(near=0;near<2;near++)for(order=1;order<=3;order+=2) {
        sp_field_cfg_t fc=fieldconfig(fs,order,order==1?SP_FIELD_AMB1:SP_FIELD_AMB3,0,64,h,14);fx_cfg_t xc=config(0,order==1?4:16,64);void *mem;
        fc.near_mm=near?2000:100;mem=initfx(fx,&xc,fs,1);need(sp_field_init(f,&fc)==0&&sp_field_attach_fx(f,fx)==0,"target init");
        for(yaw=0;yaw<2;yaw++) {
            sp_field_set_yaw(f,yaw?90000:0);sp_field_render_q30(f,NULL,0,0,out,64);
            for(b=0;b<4;b++)for(c=0;c<2;c++) {
                need(memcmp(f->wet[b][c].gt,expected[near*4+yaw*2+c],f->nsh*sizeof(int32_t))==0,"all exact wet targets from independent recurrence");
                if(!yaw)need(memcmp(f->wet[b][c].gp,f->wet[b][c].gt,sizeof f->wet[b][c].gp)==0,"wet initial no fade");
                else need(memcmp(f->wet[b][c].gp,expected[near*4+c],f->nsh*sizeof(int32_t))==0,"wet yaw ramps from previous target");
            }
        }
    free(mem);
    }
    free(fx);free(f);
}

static void prime_line(fx_line_t *l,int32_t value) { unsigned i;for(i=0;i<=l->mask;i++)l->buf[i]=value;l->store=value; }
static void ranges(unsigned fs) {
    unsigned route,pinna,c,t,b,j;int16_t h[4096]={0};
    for(route=0;route<3;route++)for(pinna=0;pinna<2;pinna++) {
        unsigned ch=route==0?4:route==1?16:2;uint64_t internal=0,final=0;int32_t raw[64];int16_t out[16];
        sp_field_t *f=calloc(1,sizeof *f);fx_t *fx=malloc(sizeof *fx);fx_cfg_t xc=config(FX_ON_BUSES|FX_ON_CRUSH|FX_ON_GAIN|FX_ON_DC,ch,128);sp_field_cfg_t fc;void *mem;
        if(!f||!fx)exit(2);
        memset(h,0,sizeof h);h[0]=-127;
        if(route==2)xc.on|=FX_ON_COMPRESSOR|FX_ON_LIMITER|FX_ON_SOFTCLIP|FX_ON_DITHER;
        xc.gain_db10=240;xc.compressor_makeup_db10=240;xc.dc_millihz=1;
        fc=fieldconfig(fs,3,route==0?SP_FIELD_AMB1:route==1?SP_FIELD_AMB3:SP_FIELD_BINAURAL,(int)pinna,128,h,1);
        mem=initfx(fx,&xc,fs,1);need(sp_field_init(f,&fc)==0&&sp_field_attach_fx(f,fx)==0,"stress admission at max H");
        for(b=0;b<FX_BUSES;b++)for(c=0;c<64;c++)fx_set_send(fx,c,b,32768);
        for(j=0;j<=fx->cho_mask;j++)fx->cho_buf[j]=INT32_MAX;
        for(j=0;j<=fx->flg_mask;j++)fx->flg_buf[j]=INT32_MIN;
        prime_line(&fx->dly,INT32_MAX);
        for(b=0;b<2;b++){for(j=0;j<8;j++)prime_line(&fx->comb[b][j],INT32_MAX);for(j=0;j<4;j++)prime_line(&fx->allp[b][j],INT32_MIN);}
        for(t=0;t<512;t++) {
            unsigned clipped,fieldclips=0;
            for(c=0;c<64;c++)raw[c]=(t&1u)?INT32_MAX:INT32_MIN;
            clipped=sp_field_render_q30(f,raw,64,64,out,1);
            if(route==2)for(c=0;c<f->nsh;c++){int64_t v=pinna?(f->acc[c][0]+(1<<20))>>21:(f->acc[c][0]+(1<<22))>>23;fieldclips+=(v>INT32_MAX||v<INT32_MIN);}
            need(clipped>=fieldclips,"separate field/final saturation accounting");internal+=fieldclips;final+=clipped-fieldclips;
            need(fx->n==t+1&&f->geo.n==t+1&&!fx->open&&!fx->piece_len,"stress counters");
            for(c=0;c<ch;c++)need(fx->dc_x1[c]>-(INT64_C(1)<<61)&&fx->dc_x1[c]<(INT64_C(1)<<61)&&fx->dc_y1[c]>-(INT64_C(1)<<61)&&fx->dc_y1[c]<(INT64_C(1)<<61),"stress DC proof bound");
            pcm_hash(out,ch);
        }
        printf("FIELD_FX_RANGE fs=%u route=%u pinna=%u field_clips=%llu output_clips=%llu intentional=1\n",fs,route,pinna,(unsigned long long)internal,(unsigned long long)final);
        need(internal+final>0,"stress actually reaches saturation");free(mem);free(fx);free(f);
    }
}
static void custom_shifts(unsigned fs) {
    static const unsigned shift[]={1,14,15,30};unsigned s,n,c;int16_t h[4096]={0};sp_field_t *a=calloc(1,sizeof *a),*b=calloc(1,sizeof *b);fx_t *fx=malloc(sizeof *fx);
    if(!a||!b||!fx)exit(2);
    h[0]=63;h[256]=-64;
    for(s=0;s<4;s++) {
        sp_field_cfg_t fc=fieldconfig(fs,3,SP_FIELD_BINAURAL,0,64,h,shift[s]);fx_cfg_t xc=config(0,2,64);void *mem=initfx(fx,&xc,fs,1);
        need(sp_field_init(a,&fc)==0&&sp_field_init(b,&fc)==0&&sp_field_attach_fx(b,fx)==0,"custom shift attach");
        sp_stereo_set_pos(&a->geo,0,-500,866,0);sp_stereo_set_pos(&b->geo,0,-500,866,0);
        for(n=0;n<257;n++) {
            int32_t x=(n%3)==0?INT32_MIN:(n%3)==1?INT32_MAX:(int32_t)n*65537-1000000;int16_t oa[2],ob[2];
            unsigned ca=sp_field_render_q30(a,&x,1,1,oa,1),cb=sp_field_render_q30(b,&x,1,1,ob,1);
            need(ca==cb&&memcmp(oa,ob,sizeof oa)==0,"custom shift/negative rounding exact bypass");
            for(c=0;c<2;c++)need(b->eo[c][0]>-(INT64_C(1)<<52)&&b->eo[c][0]<(INT64_C(1)<<52),"admitted wide ear bound");
        }
    free(mem);
    }
    free(fx);free(b);free(a);
}

static void engine_paths(unsigned fs) {
    static const unsigned ev[]={0,13,111,900,971,1205,2049,2209,4099};
    unsigned route,pin,free2,e,n,v,t;int16_t h[4096];table_fill(h);
    for(route=0;route<4;route++)for(pin=0;pin<2;pin++)for(free2=0;free2<2;free2++) {
        sp_field_t *a=calloc(1,sizeof *a),*b=calloc(1,sizeof *b);fx_t *xa=malloc(sizeof *xa),*xb=malloc(sizeof *xb);fm_patch_t patch;fm_voice_t va[2],vb[2];
        unsigned ch=route==0?4:route==1?16:2,ca=0,cb=0;int16_t oa[128*16],ob[128*16];int32_t raw[128*2],temp[128];
        sp_field_cfg_t fc=fieldconfig(fs,route==2?1:3,route==0?SP_FIELD_AMB1:route==1?SP_FIELD_AMB3:SP_FIELD_BINAURAL,(int)pin,64,h,14);fx_cfg_t xc=config(FX_ON_BUSES|FX_ON_CRUSH,ch,64);void *ma,*mb;
        if(!a||!b||!xa||!xb)exit(2);
        memset(&patch,0,sizeof patch);memset(va,0,sizeof va);memset(vb,0,sizeof vb);
        patch.op[0].ratio_q16=patch.op[1].ratio_q16=65536;patch.op[0].level_db10=240;patch.op[1].level_db10=1200;patch.op[0].release_ms=1;patch.op[0].free_run=(uint16_t)(free2?2:0);
        patch.amp.enabled=1;patch.amp.attack_ms=1;patch.amp.decay_ms=1;patch.amp.sustain_db10=10;patch.amp.release_ms=25;
        for(v=0;v<2;v++){fm_voice_init(&va[v],&patch,fs);fm_voice_init(&vb[v],&patch,fs);}
        ma=initfx(xa,&xc,fs,1);mb=initfx(xb,&xc,fs,1);need(sp_field_init(a,&fc)==0&&sp_field_init(b,&fc)==0&&sp_field_attach_fx(a,xa)==0&&sp_field_attach_fx(b,xb)==0,"engine path init");
        for(v=0;v<2;v++){unsigned bus;fx_set_crush(xa,v,10+v,13+v);fx_set_crush(xb,v,10+v,13+v);for(bus=0;bus<FX_BUSES;bus++){fx_set_send(xa,v,bus,5000+bus*1000);fx_set_send(xb,v,bus,5000+bus*1000);}}
        for(e=0;e<8;e++) {
            n=ev[e];
            if(n==13||n==900||n==2049)for(v=0;v<2;v++){fm_voice_note_on(&va[v],110000+v*37000,24000);fm_voice_note_on(&vb[v],110000+v*37000,24000);}
            if(n==111||n==971||n==2209)for(v=0;v<2;v++){fm_voice_note_off(&va[v]);fm_voice_note_off(&vb[v]);}
            if(n==1205){fx_set_crush(xa,0,8,7);fx_set_crush(xb,0,8,7);sp_field_set_yaw(a,35123);sp_field_set_yaw(b,35123);}
            if(n==900){sp_stereo_move(&a->geo,0,707,707,200,1901);sp_stereo_move(&b->geo,0,707,707,200,1901);}
            while(n<ev[e+1]) {
                unsigned len=ev[e+1]-n;
                if(len>63)len=63;
                for(v=0;v<2;v++){
                    if(fm_voice_active(&vb[v])||fm_voice_needs_clock(&vb[v]))fm_render_q30(&vb[v],temp,len);else memset(temp,0,len*sizeof *temp);
                    for(t=0;t<len;t++)raw[t*2+v]=temp[t];
                }
                ca+=sp_field_render(a,va,2,oa,len);for(t=0;t<len;t++)cb+=sp_field_render_q30(b,raw+t*2,2,2,ob+t*ch,1);
                need(ca==cb&&memcmp(oa,ob,len*ch*sizeof *oa)==0,"engine versus exact pre-insert Q30 injection");
                need(memcmp(va,vb,sizeof va)==0,"engine control state unchanged by field/FX");field_equal(a,b);fx_equal(xa,xb,ma,mb,fx_field_mem_bytes(&xc,fs));pcm_hash(ob,len*ch);n+=len;
            }
        }free(mb);free(ma);free(xb);free(xa);free(b);free(a);
    }
}

static int64_t floor_div(int64_t x,int64_t d) { int64_t q=x/d;return q-(x<0&&x%d!=0); }
/* This component oracle accepts separately validated target/tap integers and
 * actual raw network-return samples as inputs. It independently checks exact
 * pinna convolution, gain ramps and Q27/Q25 alignment, not the network law;
 * fieldr's wholly separate real signal model covers the networks end to end. */
static void discrete_returns(unsigned fs) {
    unsigned pin,n,b,ear,c,j;int16_t h[4096]={0};h[0]=127;
    for(pin=0;pin<4;pin++) {
        sp_field_t *f=calloc(1,sizeof *f);fx_t *fx=malloc(sizeof *fx);int32_t history[8][32]={ {0} };int16_t out[2];
        sp_field_cfg_t fc=fieldconfig(fs,3,SP_FIELD_BINAURAL,(int)pin,64,h,14);fx_cfg_t xc=config(FX_ON_BUSES,2,64);void *mem;
        if(!f||!fx)exit(2);
        mem=initfx(fx,&xc,fs,1);need(sp_field_init(f,&fc)==0&&sp_field_attach_fx(f,fx)==0,"discrete wet init");
        for(j=0;j<=fx->cho_mask;j++)fx->cho_buf[j]=(int32_t)(j%101)*1234567-60000000;
        for(j=0;j<=fx->flg_mask;j++)fx->flg_buf[j]=(int32_t)(j%79)*1234567-45000000;
        prime_line(&fx->dly,-123456789);for(b=0;b<2;b++){for(j=0;j<8;j++)prime_line(&fx->comb[b][j],123456789);for(j=0;j<4;j++)prime_line(&fx->allp[b][j],-123456789);}
        for(n=0;n<513;n++) {
            int64_t acc[16]={0};unsigned phase=n%64;
            if(n==127)sp_field_set_yaw(f,31007);
            if(n==309)sp_field_set_yaw(f,-127003);
            sp_field_render_q30(f,NULL,0,0,out,1);
            for(b=0;b<4;b++)for(ear=0;ear<2;ear++) {
                unsigned v=b*2+ear;const sp_fvoice_t *w=&f->wet[b][ear];int64_t x=f->wetbuf[b][ear][0];
                history[v][n%32]=(int32_t)x;
                if(pin) {
                    int64_t sum=0;
                    for(j=0;j<21;j++)sum+=floor_div((int64_t)w->hp[j]*(63-phase)+(int64_t)w->ht[j]*(phase+1),64)*history[v][(n-j)%32];
                    x=floor_div(sum+8388608,16777216);
                    need(memcmp(history[v],w->hist,sizeof history[v])==0,"wet pinna exact separate history");
                }
                for(c=0;c<16;c++)acc[c]+=x*floor_div((int64_t)w->gp[c]*(63-phase)+(int64_t)w->gt[c]*(phase+1),64)*8;
            }
            for(c=0;c<16;c++)need(acc[c]==f->acc[c][0],"independent discrete wet convolution/encoding alignment");
        }free(mem);free(fx);free(f);
    }
}
int main(void) {
    unsigned fs,k;printf("FIELD_FX sizes field=%zu fx=%zu wetbuffer=%zu\n",sizeof(sp_field_t),sizeof(fx_t),sizeof(((sp_field_t *)0)->wetbuf));
    for(fs=44100;fs<=48000;fs+=3900){attachment(fs);bypass(fs);idle(fs,0);idle(fs,SP_PINNA_PBNH);model(fs);targets(fs);ranges(fs);custom_shifts(fs);engine_paths(fs);discrete_returns(fs);for(k=32;k<=128;k*=2){split(fs,k);partitions(fs,k);}}
    printf("FIELD_FX %s checks=%lu failures=%lu digest=%016llx\n",failures?"FAIL":"ok",checks,failures,(unsigned long long)digest);return failures?1:0;
}
