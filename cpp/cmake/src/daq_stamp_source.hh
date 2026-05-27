#ifndef GDS_GUIDER_DAQ_STAMP_SOURCE_HH
#define GDS_GUIDER_DAQ_STAMP_SOURCE_HH

#include "stamp_source.hh"
#include "Decoder.hh"

#include "gds/LocationSet.hh"

#include <atomic>
#include <memory>
#include <string>
#include <thread>

namespace GDS { namespace Guider {

// Source of guider stamps backed by the GDS DAQ.
//
// Owns a Guider::Decoder and drives `wait()` on a worker thread,
// following the canonical pattern from
// R5-V13.13/examples/gds/gds_listener.cc:
//
//     while(*subscriber) subscriber->wait();
//
// Thread layout: one DaqStampSource owns one Decoder which is
// subscribed to one or more sensors. Per Gregg, callbacks within a
// single Decoder are serialized, so Decoder's single shared decode
// buffer is safe. For parallel-stream operation you would create
// multiple DaqStampSources, one per sensor, and rendezvous outside.
class DaqStampSource
{
public:
    DaqStampSource(std::string             partition,
                   const GDS::LocationSet& locations);
    ~DaqStampSource();

    // Construct a Decoder bound to `on_stamp`, spawn a worker thread
    // that runs `while(*decoder_) decoder_->wait();`. Idempotent;
    // a second call while running is a no-op.
    void start(::guider::StampCallback on_stamp);

    // Signal abort to the Decoder, join the worker, destroy the
    // Decoder. Idempotent. Called automatically by the destructor.
    void stop();

private:
    void run_loop();

    std::string              _partition;
    GDS::LocationSet         _locations;
    std::unique_ptr<Decoder> _decoder;
    std::thread              _worker;
    std::atomic<bool>        _running;
};

}}

#endif
