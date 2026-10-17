#pragma once
#include "../sink.h"
#include <chrono>
#include <cstdio>
#include <string>

namespace dsp::routing {
    template <class T>
    class Splitter : public Sink<T> {
        using base_type = Sink<T>;
    public:
        Splitter() {}

        Splitter(stream<T>* in) { base_type::init(in); }

        void setDiagnosticName(const std::string& name) { diagnosticName = name; }

        void bindStream(stream<T>* stream) {
            assert(base_type::_block_init);
            std::lock_guard<std::recursive_mutex> lck(base_type::ctrlMtx);
            
            // Check that the stream isn't already bound
            if (std::find(streams.begin(), streams.end(), stream) != streams.end()) {
                throw std::runtime_error("[Splitter] Tried to bind stream to that is already bound");
            }

            // Add to the list
            const auto diagStart = std::chrono::steady_clock::now();
            if (!diagnosticName.empty()) {
                std::fprintf(stderr, "[DSP diag] Splitter %s bind begin stream=%p outputs=%zu\n",
                             diagnosticName.c_str(), (void*)stream, streams.size());
                std::fflush(stderr);
            }
            base_type::tempStop();
            base_type::registerOutput(stream);
            streams.push_back(stream);
            base_type::tempStart();
            if (!diagnosticName.empty()) {
                const auto us = std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - diagStart).count();
                std::fprintf(stderr, "[DSP diag] Splitter %s bind end stream=%p outputs=%zu duration=%lld us\n",
                             diagnosticName.c_str(), (void*)stream, streams.size(), (long long)us);
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
            if (!diagnosticName.empty()) {
                std::fprintf(stderr, "[DSP diag] Splitter %s unbind begin stream=%p outputs=%zu\n",
                             diagnosticName.c_str(), (void*)stream, streams.size());
                std::fflush(stderr);
            }
            base_type::tempStop();
            streams.erase(sit);
            base_type::unregisterOutput(stream);
            base_type::tempStart();
            if (!diagnosticName.empty()) {
                const auto us = std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - diagStart).count();
                std::fprintf(stderr, "[DSP diag] Splitter %s unbind end stream=%p outputs=%zu duration=%lld us\n",
                             diagnosticName.c_str(), (void*)stream, streams.size(), (long long)us);
                std::fflush(stderr);
            }
        }

        int run() {
            int count = base_type::_in->read();
            if (count < 0) { return -1; }

            size_t outputIndex = 0;
            for (const auto& stream : streams) {
                memcpy(stream->writeBuf, base_type::_in->readBuf, count * sizeof(T));
                const auto swapStart = std::chrono::steady_clock::now();
                const bool swapped = stream->swap(count);
                const auto swapUs = std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - swapStart).count();
                if (!diagnosticName.empty() && (!swapped || swapUs >= 5000)) {
                    std::fprintf(stderr,
                                 "[DSP diag] Splitter %s output=%zu stream=%p swap=%d blocked=%lld us count=%d outputs=%zu\n",
                                 diagnosticName.c_str(), outputIndex, (void*)stream, swapped ? 1 : 0,
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
        std::vector<stream<T>*> streams;
        std::string diagnosticName;

    };
}