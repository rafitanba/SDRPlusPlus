#!/usr/bin/env python3
from __future__ import annotations

import shutil
from pathlib import Path

PATH = Path('source_modules/sdrplay_source/src/main.cpp')


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{label}: expected exactly one match, found {count}')
    return text.replace(old, new, 1)


def main() -> None:
    if not PATH.exists():
        raise SystemExit(f'Run this script from the SDRPlusPlus repository root; missing {PATH}')

    text = PATH.read_text(encoding='utf-8')

    if 'lifecycle-v8:' in text:
        print('lifecycle-v8 is already applied')
        return
    if 'lifecycle-v7:' not in text:
        raise RuntimeError('Expected lifecycle-v7 source. Pull fix/sdrplay-device-lifecycle first.')

    backup = PATH.with_suffix(PATH.suffix + '.v7.bak')
    if not backup.exists():
        shutil.copy2(PATH, backup)

    text = replace_once(
        text,
        '[SDRplay diag] lifecycle-v7: stream-health validation + API recovery + metadata-only selection',
        '[SDRplay diag] lifecycle-v8: forwarded-data health validation + API recovery',
        'diagnostic marker',
    )

    old_wait = '''    bool waitForHealthyCallbackFlow(const char* context, uint64_t callbacksBeforeInit) {
        // A healthy 6 MS/s RSP1B produces callbacks very rapidly. We only require a tiny
        // number so this remains conservative at lower sample rates.
        constexpr uint64_t MIN_CALLBACKS = 8;
        constexpr int MAX_WAIT_MS = 750;

        for (int waited = 0; waited < MAX_WAIT_MS; waited += 10) {
            const uint64_t nowCount = streamCallbackCount.load();
            if (nowCount >= callbacksBeforeInit + MIN_CALLBACKS) {
                std::fprintf(stderr,
                             "[SDRplay diag] %s: stream health OK (%llu callbacks after Init in <=%d ms)\\n",
                             context,
                             (unsigned long long)(nowCount - callbacksBeforeInit),
                             waited + 10);
                std::fflush(stderr);
                return true;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }

        const uint64_t finalCount = streamCallbackCount.load();
        std::fprintf(stderr,
                     "[SDRplay diag] %s: STREAM HEALTH FAIL (%llu callbacks in %d ms after successful Init)\\n",
                     context,
                     (unsigned long long)(finalCount - callbacksBeforeInit),
                     MAX_WAIT_MS);
        std::fflush(stderr);
        return false;
    }
'''

    new_wait = '''    bool waitForHealthyDataFlow(const char* context,
                                  uint64_t forwardedBeforeInit,
                                  uint64_t swapsBeforeInit) {
        // Raw API callbacks are not enough: callbacks received while the source is not
        // accepting samples are deliberately discarded. Require actual callbacks to be
        // forwarded into SDR++ and at least one successful stream.swap(), then verify that
        // the flow remains active for a second observation window.
        constexpr uint64_t MIN_FORWARDED_CALLBACKS = 8;
        constexpr uint64_t MIN_SWAPS = 1;
        constexpr int INITIAL_WAIT_MS = 750;
        constexpr int SUSTAIN_MS = 300;

        bool initialFlow = false;
        for (int waited = 0; waited < INITIAL_WAIT_MS; waited += 10) {
            const uint64_t forwarded = forwardedCallbackCount.load();
            const uint64_t swaps = successfulSwapCount.load();
            if (forwarded >= forwardedBeforeInit + MIN_FORWARDED_CALLBACKS &&
                swaps >= swapsBeforeInit + MIN_SWAPS) {
                initialFlow = true;
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }

        if (!initialFlow) {
            std::fprintf(stderr,
                         "[SDRplay diag] %s: DATA HEALTH FAIL during initial window "
                         "(raw=%llu forwarded=%llu swaps=%llu in %d ms)\\n",
                         context,
                         (unsigned long long)streamCallbackCount.load(),
                         (unsigned long long)(forwardedCallbackCount.load() - forwardedBeforeInit),
                         (unsigned long long)(successfulSwapCount.load() - swapsBeforeInit),
                         INITIAL_WAIT_MS);
            std::fflush(stderr);
            return false;
        }

        const uint64_t forwardedAtSustainStart = forwardedCallbackCount.load();
        const uint64_t swapsAtSustainStart = successfulSwapCount.load();
        std::this_thread::sleep_for(std::chrono::milliseconds(SUSTAIN_MS));

        const uint64_t forwardedNow = forwardedCallbackCount.load();
        const uint64_t swapsNow = successfulSwapCount.load();
        const uint64_t now = monotonicMs();
        const uint64_t lastCb = lastStreamCallbackMs.load();
        const uint64_t callbackAge = lastCb ? (now - lastCb) : 999999;

        const bool sustained =
            forwardedNow >= forwardedAtSustainStart + MIN_FORWARDED_CALLBACKS &&
            swapsNow >= swapsAtSustainStart + MIN_SWAPS &&
            callbackAge <= 100;

        std::fprintf(stderr,
                     "[SDRplay diag] %s: %s "
                     "(forwarded +%llu, swaps +%llu, last callback %llu ms ago over %d ms)\\n",
                     context,
                     sustained ? "data health OK" : "DATA HEALTH FAIL during sustain window",
                     (unsigned long long)(forwardedNow - forwardedAtSustainStart),
                     (unsigned long long)(swapsNow - swapsAtSustainStart),
                     (unsigned long long)callbackAge,
                     SUSTAIN_MS);
        std::fflush(stderr);
        return sustained;
    }
'''
    text = replace_once(text, old_wait, new_wait, 'health-check function')

    old_normal = '''        const uint64_t callbacksBeforeInit = streamCallbackCount.load();
        err = sdrplay_api_Init(openDev.dev, &cbFuncs, this);
        if (err != sdrplay_api_Success) {
            flog::error("[SDRplay diag] {0}: Init failed: {1}",
                        context, sdrplay_api_GetErrorString(err));
            sdrplay_api_ErrT releaseErr = sdrplay_api_ReleaseDevice(&openDev);
            flog::warn("[SDRplay diag] {0}: ReleaseDevice after Init failure: {1}",
                       context, sdrplay_api_GetErrorString(releaseErr));
            return false;
        }

        if (!waitForHealthyCallbackFlow(context, callbacksBeforeInit)) {
            sdrplay_api_ErrT uninitErr = sdrplay_api_Uninit(openDev.dev);
            flog::warn("[SDRplay diag] {0}: Uninit after stream health failure: {1}",
                       context, sdrplay_api_GetErrorString(uninitErr));
            sdrplay_api_ErrT releaseErr = sdrplay_api_ReleaseDevice(&openDev);
            flog::warn("[SDRplay diag] {0}: ReleaseDevice after stream health failure: {1}",
                       context, sdrplay_api_GetErrorString(releaseErr));
            return false;
        }

        flog::info("[SDRplay diag] {0}: Init succeeded with healthy callback flow", context);
        return true;
'''

    new_normal = '''        // The callback starts as part of Init(). Prepare the SDR++ output path before
        // calling Init so health validation observes samples that actually reach the DSP
        // pipeline rather than callbacks that are discarded while running == false.
        bufferIndex = 0;
        bufferSize = (float)sampleRate / 200.0f;
        sampleSequenceValid = false;
        stream.clearWriteStop();
        const uint64_t forwardedBeforeInit = forwardedCallbackCount.load();
        const uint64_t swapsBeforeInit = successfulSwapCount.load();
        acceptSamples.store(true);

        err = sdrplay_api_Init(openDev.dev, &cbFuncs, this);
        if (err != sdrplay_api_Success) {
            acceptSamples.store(false);
            stream.stopWriter();
            flog::error("[SDRplay diag] {0}: Init failed: {1}",
                        context, sdrplay_api_GetErrorString(err));
            sdrplay_api_ErrT releaseErr = sdrplay_api_ReleaseDevice(&openDev);
            flog::warn("[SDRplay diag] {0}: ReleaseDevice after Init failure: {1}",
                       context, sdrplay_api_GetErrorString(releaseErr));
            stream.clearWriteStop();
            return false;
        }

        if (!waitForHealthyDataFlow(context, forwardedBeforeInit, swapsBeforeInit)) {
            acceptSamples.store(false);
            stream.stopWriter();
            sdrplay_api_ErrT uninitErr = sdrplay_api_Uninit(openDev.dev);
            flog::warn("[SDRplay diag] {0}: Uninit after data health failure: {1}",
                       context, sdrplay_api_GetErrorString(uninitErr));
            sdrplay_api_ErrT releaseErr = sdrplay_api_ReleaseDevice(&openDev);
            flog::warn("[SDRplay diag] {0}: ReleaseDevice after data health failure: {1}",
                       context, sdrplay_api_GetErrorString(releaseErr));
            stream.clearWriteStop();
            return false;
        }

        flog::info("[SDRplay diag] {0}: Init succeeded with healthy forwarded data", context);
        return true;
'''
    text = replace_once(text, old_normal, new_normal, 'normal Init health block')

    old_hard = '''        const uint64_t callbacksBeforeInit = streamCallbackCount.load();
        err = sdrplay_api_Init(openDev.dev, &cbFuncs, this);
        if (err != sdrplay_api_Success) {
            flog::error("[SDRplay diag] HARD RECOVERY: Init failed: {0}",
                        sdrplay_api_GetErrorString(err));
            sdrplay_api_ErrT releaseErr = sdrplay_api_ReleaseDevice(&openDev);
            flog::warn("[SDRplay diag] HARD RECOVERY: ReleaseDevice after Init failure: {0}",
                       sdrplay_api_GetErrorString(releaseErr));
            return false;
        }

        if (!waitForHealthyCallbackFlow("HARD RECOVERY", callbacksBeforeInit)) {
            sdrplay_api_ErrT uninitErr = sdrplay_api_Uninit(openDev.dev);
            flog::warn("[SDRplay diag] HARD RECOVERY: Uninit after stream health failure: {0}",
                       sdrplay_api_GetErrorString(uninitErr));
            sdrplay_api_ErrT releaseErr = sdrplay_api_ReleaseDevice(&openDev);
            flog::warn("[SDRplay diag] HARD RECOVERY: ReleaseDevice after stream health failure: {0}",
                       sdrplay_api_GetErrorString(releaseErr));
            return false;
        }

        std::fprintf(stderr, "[SDRplay diag] HARD RECOVERY SUCCESS: fresh API session initialized with healthy callbacks\\n");
        std::fflush(stderr);
        return true;
'''

    new_hard = '''        bufferIndex = 0;
        bufferSize = (float)sampleRate / 200.0f;
        sampleSequenceValid = false;
        stream.clearWriteStop();
        const uint64_t forwardedBeforeInit = forwardedCallbackCount.load();
        const uint64_t swapsBeforeInit = successfulSwapCount.load();
        acceptSamples.store(true);

        err = sdrplay_api_Init(openDev.dev, &cbFuncs, this);
        if (err != sdrplay_api_Success) {
            acceptSamples.store(false);
            stream.stopWriter();
            flog::error("[SDRplay diag] HARD RECOVERY: Init failed: {0}",
                        sdrplay_api_GetErrorString(err));
            sdrplay_api_ErrT releaseErr = sdrplay_api_ReleaseDevice(&openDev);
            flog::warn("[SDRplay diag] HARD RECOVERY: ReleaseDevice after Init failure: {0}",
                       sdrplay_api_GetErrorString(releaseErr));
            stream.clearWriteStop();
            return false;
        }

        if (!waitForHealthyDataFlow("HARD RECOVERY", forwardedBeforeInit, swapsBeforeInit)) {
            acceptSamples.store(false);
            stream.stopWriter();
            sdrplay_api_ErrT uninitErr = sdrplay_api_Uninit(openDev.dev);
            flog::warn("[SDRplay diag] HARD RECOVERY: Uninit after data health failure: {0}",
                       sdrplay_api_GetErrorString(uninitErr));
            sdrplay_api_ErrT releaseErr = sdrplay_api_ReleaseDevice(&openDev);
            flog::warn("[SDRplay diag] HARD RECOVERY: ReleaseDevice after data health failure: {0}",
                       sdrplay_api_GetErrorString(releaseErr));
            stream.clearWriteStop();
            return false;
        }

        std::fprintf(stderr, "[SDRplay diag] HARD RECOVERY SUCCESS: fresh API session initialized with healthy forwarded data\\n");
        std::fflush(stderr);
        return true;
'''
    text = replace_once(text, old_hard, new_hard, 'hard-recovery Init health block')

    old_post = '''        _this->channelParams = _this->openDevParams->rxChannelA;

        // Configure device
        _this->bufferIndex = 0;
        _this->bufferSize = (float)_this->sampleRate / 200.0f;
'''
    new_post = '''        _this->channelParams = _this->openDevParams->rxChannelA;

        // The sample buffer was initialized before Init so callbacks could be validated
        // against the real SDR++ DSP path.
'''
    text = replace_once(text, old_post, new_post, 'post-Init buffer reset')

    old_stop = '''        flog::info("[SDRplay diag] Stop requested");
        _this->running = false;
        _this->stream.stopWriter();
'''
    new_stop = '''        flog::info("[SDRplay diag] Stop requested");
        _this->running = false;
        _this->acceptSamples.store(false);
        _this->stream.stopWriter();
'''
    text = replace_once(text, old_stop, new_stop, 'Stop sample gate')

    old_guard = '''        if (_this->sampleSequenceValid && params->firstSampleNum != _this->expectedSampleNum) {'''
    new_guard = '''        if (!_this->acceptSamples.load()) { return; }

        if (_this->sampleSequenceValid && params->firstSampleNum != _this->expectedSampleNum) {'''
    text = replace_once(text, old_guard, new_guard, 'callback accept-samples gate')

    old_running_guard = '''        // TODO: Optimise using volk and math
        if (!_this->running) { return; }
        for (int i = 0; i < numSamples; i++) {'''
    new_running_guard = '''        // TODO: Optimise using volk and math
        for (int i = 0; i < numSamples; i++) {'''
    text = replace_once(text, old_running_guard, new_running_guard, 'remove running callback gate')

    old_swap_tail = '''                _this->bufferIndex = 0;
                if (!swapped) { return; }
            }
        }
    }
'''
    new_swap_tail = '''                _this->bufferIndex = 0;
                if (!swapped) { return; }
                _this->successfulSwapCount.fetch_add(1);
            }
        }
        _this->forwardedCallbackCount.fetch_add(1);
    }
'''
    text = replace_once(text, old_swap_tail, new_swap_tail, 'forwarded callback/swap counters')

    old_members = '''    std::atomic<uint64_t> lastStreamCallbackMs{0};
    std::atomic<uint64_t> streamCallbackCount{0};
    std::atomic<bool> inStreamSwap{false};'''
    new_members = '''    std::atomic<uint64_t> lastStreamCallbackMs{0};
    std::atomic<uint64_t> streamCallbackCount{0};
    std::atomic<uint64_t> forwardedCallbackCount{0};
    std::atomic<uint64_t> successfulSwapCount{0};
    std::atomic<bool> acceptSamples{false};
    std::atomic<bool> inStreamSwap{false};'''
    text = replace_once(text, old_members, new_members, 'stream-health members')

    PATH.write_text(text, encoding='utf-8')
    print(f'Applied lifecycle-v8 to {PATH}')
    print(f'Backup: {backup}')


if __name__ == '__main__':
    main()
