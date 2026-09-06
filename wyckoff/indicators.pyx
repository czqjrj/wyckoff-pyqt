# cython: boundscheck=False, wraparound=False, cdivision=True
# distutils: language_level = "3"

import numpy as np
cimport numpy as np
from libc.math cimport fabs

cdef double _alpha(int span):
    """计算 EWMA alpha。"""
    return 2.0 / (span + 1)


def _ewma_cython(arr, span):
    """Cython 优化版 EWMA。
    接受任意 NumPy 数组，返回 EWMA 序列。
    """
    cdef int n = len(arr)
    cdef np.ndarray[double] out = np.empty(n, dtype=np.float64)
    cdef double alpha = _alpha(span)
    cdef double prev, curr
    cdef int i

    out[0] = arr[0]
    for i in range(1, n):
        cdef double a = arr[i]
        cdef double p = out[i - 1]
        out[i] = alpha * a + (1 - alpha) * p
    return out


cdef np.ndarray _rolling_mean_cython(arr, window):
    """Cython 前缀和滑动平均。"""
    cdef int n = len(arr)
    cdef np.ndarray out = np.full(n, np.nan)
    cdef np.ndarray clean = np.nan_to_num(arr, nan=0.0)
    cdef np.ndarray cs = np.concatenate(([0.0], np.cumsum(clean)))
    cdef np.ndarray csm = np.concatenate(([0.0], np.cumsum(
        np.isfinite(arr).astype(float))))
    cdef double win_sum, win_n
    cdef int i, ok

    for i in range(window, n):
        win_sum = cs[i + 1] - cs[i + 1 - window]
        win_n = csm[i + 1] - csm[i + 1 - window]
        ok = win_n >= window
        out[i] = np.where(ok, win_sum / np.where(win_n > 0, winstreading`

Let me verify the Cython compiled code works, and then run the fullnan