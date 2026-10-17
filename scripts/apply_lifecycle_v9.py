#!/usr/bin/env python3
from pathlib import Path

PATH = Path("source_modules/sdrplay_source/src/main.cpp")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


text = PATH.read_text()
text = replace_once(
    text,
    "[SDRplay diag] lifecycle-v8: forwarded-data health validation + API recovery",
    "[SDRplay diag] lifecycle-v9: forwarded-data validation + redundant-tune guard + API recovery",
    "marker",
)

old = '''    static void tune(double freq, void* ctx) {
        SDRPlaySourceModule* _this = (SDRPlaySourceModule*)ctx;
        if (_this->running) {
            _this->channelParams->tunerParams.rfFreq.rfHz = freq;
            sdrplay_api_Update(_this->openDev.dev, _this->openDev.tuner, sdrplay_api_Update_Tuner_Frf, sdrplay_api_Update_Ext1_None);
        }
        _this->freq = freq;
        flog::info("SDRPlaySourceModule '{0}': Tune: {1}!", _this->name, freq);
    }
'''

new = '''    static void tune(double freq, void* ctx) {
        SDRPlaySourceModule* _this = (SDRPlaySourceModule*)ctx;
        const double previousFreq = _this->freq;
        _this->freq = freq;

        if (_this->running) {
            double delta = freq - previousFreq;
            if (delta < 0.0) { delta = -delta; }

            // MainWindow calls tune() immediately after start(), even though the same RF
            // frequency was already supplied before Init. Avoid a redundant Frf update at
            // exactly the point where otherwise healthy callback flow has been disappearing.
            if (delta < 1.0) {
                std::fprintf(stderr,
                             "[SDRplay diag] Tune skipped: unchanged RF %.0f Hz (callbacks=%llu swaps=%llu)\\n",
                             freq,
                             (unsigned long long)_this->streamCallbackCount.load(),
                             (unsigned long long)_this->successfulSwapCount.load());
                std::fflush(stderr);
                return;
            }

            _this->channelParams->tunerParams.rfFreq.rfHz = freq;
            const sdrplay_api_ErrT err = sdrplay_api_Update(
                _this->openDev.dev,
                _this->openDev.tuner,
                sdrplay_api_Update_Tuner_Frf,
                sdrplay_api_Update_Ext1_None);
            std::fprintf(stderr,
                         "[SDRplay diag] Tune update: %.0f -> %.0f Hz, result=%s, callbacks=%llu swaps=%llu\\n",
                         previousFreq, freq, sdrplay_api_GetErrorString(err),
                         (unsigned long long)_this->streamCallbackCount.load(),
                         (unsigned long long)_this->successfulSwapCount.load());
            std::fflush(stderr);
        }

        flog::info("SDRPlaySourceModule '{0}': Tune: {1}!", _this->name, freq);
    }
'''

text = replace_once(text, old, new, "tune block")
PATH.write_text(text)
