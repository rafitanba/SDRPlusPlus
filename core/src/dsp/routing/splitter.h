#pragma once
#include "../sink.h"
#include <chrono>
#include <cstdio>
#include <string>
#include <unordered_map>
#include <mutex>

namespace dsp::routing {
    template <class T>
    class Splitter : public Sink<T> {
        using base_type = Sink<T>;
    public:
        Splitter() {}

        Splitter(stream<T>* in) { base_type::init(in); }

        void setDiagnosticName(const std::string& name) { diagnosticNames()[this] = name; }

        void bindStream(stream<T>* stream) {
            assert(base_type::_block_init);
            std::lock_guard<std::recursive_mutex> lck(base_type::ctrlMtx);
            
            // Check that the stream isn't already bound
            if (std::find(streams.begin(), streams.end(), stream) != streams.end()) {
                throw std::runtime_error("[Splitter] Tried to bind stream to that is already bound");
            }

            // Add to the list
            const auto diagStart = std::chrono::steady_clock::now();
            if (!getDiagnosticName().empty()) {
                std::fprintf(stderr, "[DSP diag] Splitter %s bind begin stream=%p outputs=%zu\n",
                             getDiagnosticName().c_str(), (void*)stream, streams.size());
                std::fflush(stderr);
            }
            base_type::tempStop();
            base_type::registerOutput(stream);
            streams.push_back(stream);
            base_type::tempStart();
            if (!getDiagnosticName().empty()) {
                const auto us = std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - diagStart).count();
                std::fprintf(stderr, "[DSP diag] Splitter %s bind end stream=%p outputs=%zu duration=%lld us\n",
                             getDiagnosticName().c_str(), (void*)stream, streams.size(), (long long)us);
                std::fflush(stderr);
            }
        }

        void unbindStream(stream<T>* stream) {
            assert(base_type::_block_init);
            std::lock_guard<std::recursive_mutex> lck(base_type::ctrlMtx);
            
            // Check that the stream is bound
            auto sit = std::find(streams.begin(), streams.end(), stream);
            if (sit == streams.end()) {
                throw std::runtime_error("[Splitter] Tried to unbind stream to that isn't bound");
            }

            // Remove the stream while the splitter worker is stopped.
            const auto diagStart = std::chrono::steady_clock::now();
            if (!getDiagnosticName().empty()) {
                std::fprintf(stderr, "[DSP diag] Splitter %s unbind begin stream=%p outputs=%zu\n",
                             getDiagnosticName().c_str(), (void*)stream, streams.size());
                std::fflush(stderr);
            }
            base_type::tempStop();
            streams.erase(sit);
            base_type::unregisterOutput(stream);
            clearTimeoutCount(stream);
            base_type::tempStart();
            if (!getDiagnosticName().empty()) {
                const auto us = std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - diagStart).count();
                std::fprintf(stderr, "[DSP diag] Splitter %s unbind end stream=%p outputs=%zu duration=%lld us\n",
                             getDiagnosticName().c_str(), (void*)stream, streams.size(), (long long)us);
                std::fflush(stderr);
            }
        }

        int run() {
            int count = base_type::_in->read();
            if (count < 0) { return -1; }

            // The main IQ fan-out must never let a dead VFO block the FFT and every
            // other VFO indefinitely. Its buffers represent roughly 5 ms at the
            // current source settings, so waiting 2 ms for an already outstanding
            // buffer is enough to distinguish a healthy consumer from a stalled one.
            const bool isolateSlowOutputs = (getDiagnosticName() == "IQFrontEnd");
            constexpr auto IQ_OUTPUT_WAIT = std::chrono::microseconds(2000);

            size_t outputIndex = 0;
            for (const auto& stream : streams) {
                memcpy(stream->writeBuf, base_type::_in->readBuf, count * sizeof(T));
                const auto swapStart = std::chrono::steady_clock::now();

                bool swapped = false;
                bool timedOut = false;

                if (isolateSlowOutputs) {
                    const auto result = stream->swapFor(count, IQ_OUTPUT_WAIT);
                    swapped = (result == dsp::stream<T>::TimedSwapResult::SWAPPED);
                    timedOut = (result == dsp::stream<T>::TimedSwapResult::TIMEOUT);
                }
                else {
                    swapped = stream->swap(count);
                }

                const auto swapUs = std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - swapStart).count();

                if (timedOut) {
                    // Count total dropped frames for this output rather than resetting on
                    // every one-frame recovery. Otherwise an alternating slow consumer
                    // floods stderr with a "drop=1" line hundreds of times per second.
                    const uint64_t drops = incrementTimeoutCount(stream);
                    if (drops == 1 || (drops % 1000) == 0) {
                        std::fprintf(stderr,
                                     "[DSP diag] Splitter %s output=%zu stream=%p DROP slow consumer blocked>%lld us count=%d outputs=%zu totalDrops=%llu\n",
                                     getDiagnosticName().c_str(), outputIndex, (void*)stream,
                                     (long long)IQ_OUTPUT_WAIT.count(), count, streams.size(),
                                     (unsigned long long)drops);
                        std::fflush(stderr);
                    }
                    outputIndex++;
                    continue;
                }

                if (!getDiagnosticName().empty() && (!swapped || swapUs >= 5000)) {
                    std::fprintf(stderr,
                                 "[DSP diag] Splitter %s output=%zu stream=%p swap=%d blocked=%lld us count=%d outputs=%zu\n",
                                 getDiagnosticName().c_str(), outputIndex, (void*)stream, swapped ? 1 : 0,
                                 (long long)swapUs, count, streams.size());
                    std::fflush(stderr);
                }
                if (!swapped) {
                    base_type::_in->flush();
                    return -1;
                }
                outputIndex++;
            }

            base_type::_in->flush();

            return count;
        }

    protected:
        static std::unordered_map<const Splitter<T>*, std::string>& diagnosticNames() {
            static std::unordered_map<const Splitter<T>*, std::string> names;
            return names;
        }

        const std::string& getDiagnosticName() const {
            static const std::string empty;
            auto& names = diagnosticNames();
            auto it = names.find(this);
            return (it == names.end()) ? empty : it->second;
        }

        static std::unordered_map<const stream<T>*, uint64_t>& timeoutCounts() {
            static std::unordered_map<const stream<T>*, uint64_t> counts;
            return counts;
        }

        static std::mutex& timeoutCountsMutex() {
            static std::mutex mtx;
            return mtx;
        }

        static uint64_t incrementTimeoutCount(const stream<T>* stream) {
            std::lock_guard<std::mutex> lck(timeoutCountsMutex());
            return ++timeoutCounts()[stream];
        }

        static void clearTimeoutCount(const stream<T>* stream) {
            std::lock_guard<std::mutex> lck(timeoutCountsMutex());
            timeoutCounts().erase(stream);
        }

        std::vector<stream<T>*> streams;

    };
}