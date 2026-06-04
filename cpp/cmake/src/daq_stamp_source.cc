#include "daq_stamp_source.hh"

#include <cstdio>
#include <exception>
#include <utility>

namespace GDS { namespace Guider {

DaqStampSource::DaqStampSource(std::string             partition,
                               const GDS::LocationSet& locations) :
    _partition(std::move(partition)),
    _locations(locations),
    _decoder  (nullptr),
    _worker   (),
    _running  (false)
{
}

DaqStampSource::~DaqStampSource()
{
    unsubscribe();
}

void DaqStampSource::subscribe(::guider::StampCallback on_stamp)
{
    if (_running.exchange(true))
    {
        return;
    }

    try
    {
        _decoder = std::make_unique<Decoder>(_partition.c_str(),
                                             _locations,
                                             std::move(on_stamp));
        _worker  = std::thread(&DaqStampSource::run_loop, this);
    }
    catch (...)
    {
        // Roll back so a future subscribe() can retry.
        _running.store(false);
        _decoder.reset();
        throw;
    }
}

void DaqStampSource::unsubscribe()
{
    if (!_running.exchange(false))
    {
        return;
    }

    if (_decoder)
    {
        _decoder->abort();
    }

    if (_worker.joinable())
    {
        _worker.join();
    }

    _decoder.reset();
}

void DaqStampSource::run_loop()
{
    // Single worker thread per DaqStampSource. Per Gregg (2026-05-08),
    // GDS::Decoder serializes its callbacks for all sensors it is
    // subscribed to, so one thread + one decode buffer is safe even
    // with multi-sensor subscriptions. Loop shape mirrors
    // R5-V13.16/examples/gds/gds_listener.cc:69.
    //
    // Any exception escaping wait() (e.g. allocate() invariant break)
    // is caught here so it does not propagate out of the std::thread
    // (which would call std::terminate). The loop exits and the next
    // unsubscribe() / dtor cleans up; Python-side will see no further stamps.
    if (!_decoder) return;

    try
    {
        while (*_decoder)
        {
            _decoder->wait();
        }
    }
    catch (const std::exception& err)
    {
        std::fprintf(stderr,
                     "DaqStampSource worker stopped on exception: %s\n",
                     err.what());
    }
    catch (...)
    {
        std::fprintf(stderr,
                     "DaqStampSource worker stopped on unknown exception\n");
    }
}

}}
