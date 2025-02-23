#pragma once
// Changed from x.type().is_cuda() due to deprecation
#define CHECK_CUDA(x) TORCH_CHECK(x.is_cuda(), #x " must be a CUDA tensor")
#define CHECK_CPU(x) TORCH_CHECK(!x.is_cuda(), #x " must be a CPU tensor")
#define CHECK_CONTIGUOUS(x)                                                    \
  TORCH_CHECK(x.is_contiguous(), #x " must be contiguous")
#define CHECK_INPUT(x)                                                         \
  CHECK_CUDA(x);                                                               \
  CHECK_CONTIGUOUS(x)
#define CHECK_CPU_INPUT(x)                                                     \
  CHECK_CPU(x);                                                                \
  CHECK_CONTIGUOUS(x)

#if defined(__CUDACC__)
// #define _EXP(x) expf(x) // SLOW EXP
#define _EXP(x) __expf(x) // FAST EXP
#define _LOG(x) __logf(x) // FAST LOG
#define _SIGMOID(x) (1. / (1. + _EXP(-(x))))

#else

#define _EXP(x) expf(x)
#define _LOG(x) logf(x) // slow LOG
#define _SIGMOID(x) (1 / (1 + expf(-(x))))

#endif

#define _NORM3(n3) sqrtf(1e-9f + _SQR(n3[0]) + _SQR(n3[1]) + _SQR(n3[2])) // TODO: use normf?
#define _D_SIGMOID(x) (_SIGMOID(x) * (1-_SIGMOID(x))) // x is the raw value (before sigmoid)
#define _SQR(x) ((x) * (x))
#define _CUBIC(x) ((x) * (x) * (x))
#define _CLOSE_TO_ZERO(x, eps) (std::abs(x) < eps)
#define _COND_CBRT(x) ((x >= 0) ? cbrt(x) : -1. * cbrt(-x))
#define _COND_CBRTF(x) ((x >= 0) ? cbrtf(x) : -1. * cbrtf(-x))
// derivative of cond_cbrt
#define _D_COND_CBRT(x) ((x >= 0) ? ( (x>0) ? (1. / 3. / _SQR(cbrt(x))) : 1.) :  cbrt(-x) / -3. / x) 
