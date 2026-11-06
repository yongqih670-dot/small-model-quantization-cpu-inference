/* b11347 Windows x64: forward this one public API, measure the original call. */
#include <windows.h>
#include <stdio.h>
#include "llama.h"

typedef struct llama_model * (*load_model_fn)(const char *, struct llama_model_params);

struct llama_model * llama_model_load_from_file(const char * path, struct llama_model_params params) {
    HMODULE original = GetModuleHandleW(L"llama_original.dll");
    if (!original) original = LoadLibraryW(L"llama_original.dll");
    load_model_fn load = original ? (load_model_fn)GetProcAddress(original, "llama_model_load_from_file") : NULL;
    LARGE_INTEGER hz, begin, end;
    if (!load || !QueryPerformanceFrequency(&hz)) {
        fprintf(stderr, "stage7: original function or performance counter unavailable\n");
        return NULL;
    }
    QueryPerformanceCounter(&begin);
    struct llama_model * model = load(path, params);
    QueryPerformanceCounter(&end);
    fprintf(stderr, "STAGE7_LOAD ticks=%lld hz=%lld ok=%d no_alloc=%d vocab_only=%d\n",
            (long long)(end.QuadPart - begin.QuadPart), (long long)hz.QuadPart,
            model != NULL, (int)params.no_alloc, (int)params.vocab_only);
    fflush(stderr);
    return model;
}
