#include "Decoder.hh"

#include "daq/Sensor.hh"
#include "gds/RawStamp.hh"
#include "gds/RoiCommon.hh"
#include "gds/SeriesMetadata.hh"

#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <utility>

using namespace GDS;

Guider::Decoder::Decoder(const char*              partition,
                         const GDS::LocationSet&  locations,
                         ::guider::StampCallback  on_stamp) :
    GDS::Decoder(partition, locations),
    _on_stamp   (std::move(on_stamp)),
    _n_rows     (0),
    _n_cols     (0),
    _stamp_size (0),
    _rstamp_size(0),
    _stamp_buf  ()
{
    for (int i = 0; i < Set::SIZE; ++i)
    {
        _stamps  [i] = 0;
        _rstamps [i] = 0;
        _segment [i] = 0;
        _startrow[i] = 0;
        _startcol[i] = 0;
    }
}

void Guider::Decoder::start(const GDS::StateMetadata&  state,
                            const GDS::SeriesMetadata& series)
{
    state .dump();
    series.dump();

    _n_rows      = series.common().nrows();
    _n_cols      = series.common().ncols();
    _stamp_size  = series.common().pixels() * sizeof(int32_t);
    _rstamp_size = RawStamp::calc_size(series);

    // The segment (amplifier) and window origin live in the per-series ROI
    // location, not in the per-stamp StateMetadata. Cache them per sensor so
    // stamp() can attach them to every stamp of this series; downstream they
    // form the (segment, startrow, startcol) key that detects an ROI change.
    _segment [state.sensor().index()] = series.location().segment();
    _startrow[state.sensor().index()] = series.location().startrow();
    _startcol[state.sensor().index()] = series.location().startcol();

    // The series id lives in the per-series metadata. A series (start) is a
    // set of ROI parameters (tied to initGuider), not an image: per Gregg
    // (2026-07-06) CCS does not yet know the image name at initGuider, and
    // one ROI can host multiple images. So series.id() is the ROI/config
    // identity, not the image name; the image name arrives on resume().
    // Cache it per sensor and clear the stale image name until resume()
    // supplies the new one.
    _series_id[state.sensor().index()] = series.id();
    _obs_id   [state.sensor().index()].clear();

    _stamp_buf.resize(_stamp_size);
}

void Guider::Decoder::resume(const GDS::StateMetadata& state)
{
    state.dump();

    // The image name (OBSID) travels on the resume command's comment, not
    // the series metadata (per Gregg, 2026-07-06). A new resume within the
    // same ROI series means a new image, so cache it per sensor here; stamp()
    // attaches it to every stamp and downstream code triggers a new coadd
    // when it changes.
    _obs_id [state.sensor().index()] = state.comment();
    _begin  [state.sensor().index()] = state.timestamp();
    _stamps [state.sensor().index()] = 0;
    _rstamps[state.sensor().index()] = 0;
}

void Guider::Decoder::pause(const GDS::StateMetadata& state)
{
    state.dump();

    uint64_t begin = (uint64_t)_begin[state.sensor().index()];
    uint64_t end   = (uint64_t)state.timestamp();
    double   diff  = (end - begin) / 1E9;

    unsigned stamps  = _stamps [state.sensor().index()];
    unsigned rstamps = _rstamps[state.sensor().index()];
    double   freq    = rstamps / diff;

    printf("  %s: (%i) %i (raw) stamps in %f sec = %f Hz\n",
           state.sensor().encode(), rstamps, stamps, diff, freq);
}

void Guider::Decoder::stop(const GDS::StateMetadata& state)
{
    state.dump();

    unsigned stamps  = _stamps [state.sensor().index()];
    unsigned rstamps = _rstamps[state.sensor().index()];

    printf("  %s: (%i) %i (raw) stamps. Errors (xfer, size, beg, end) (%u, %u, %u, %u)\n",
           state.sensor().encode(), rstamps, stamps,
           err_xfer    (state.sensor()),
           err_size    (state.sensor()),
           err_miss_beg(state.sensor()),
           err_miss_end(state.sensor()));
}

void Guider::Decoder::raw_stamp(const GDS::StateMetadata& state,
                                const GDS::RawStamp&      stamp)
{
    ++_rstamps[state.sensor().index()];

    if (_rstamp_size != stamp.size())
    {
        printf("ERROR - %s: Sequence %i, Stamp %i - Expected raw stamp size %u, received %u.\n",
               state.sensor().encode(), state.sequence(),
               _rstamps[state.sensor().index()],
               _rstamp_size, stamp.size());
    }
}

void Guider::Decoder::stamp(const GDS::StateMetadata& state,
                            const GDS::Stamp&         gds_stamp)
{
    ++_stamps[state.sensor().index()];

    if (_stamp_size != gds_stamp.size())
    {
        printf("ERROR - %s: Sequence %i, Stamp %i - Expected stamp size %u, received %u\n",
               state.sensor().encode(), state.sequence(),
               _stamps[state.sensor().index()],
               _stamp_size, gds_stamp.size());
        return;
    }

    if (!_on_stamp) return;

    // GDS::Stamp wraps the buffer we handed back from allocate(). Use
    // content() (const accessor) rather than pixels() (non-const) and
    // reinterpret to int32_t. The pointer is borrowed for this call
    // only; see the lifetime note on guider::Stamp::pixels in
    // stamp_source.hh.
    ::guider::Stamp our_stamp;
    our_stamp.pixels                = reinterpret_cast<const int32_t*>(gds_stamp.content());
    our_stamp.rows                  = _n_rows;
    our_stamp.cols                  = _n_cols;
    our_stamp.metadata.timestamp_ns = static_cast<uint64_t>(state.timestamp());
    our_stamp.metadata.sensor_index = state.sensor().index();
    our_stamp.metadata.sequence     = state.sequence();
    our_stamp.metadata.stamp_index  = state.stamp();
    our_stamp.metadata.sensor_name  = state.sensor().encode();
    our_stamp.metadata.segment      = _segment[state.sensor().index()];
    our_stamp.metadata.startrow     = _startrow[state.sensor().index()];
    our_stamp.metadata.startcol     = _startcol[state.sensor().index()];
    our_stamp.metadata.obs_id       = _obs_id[state.sensor().index()];
    our_stamp.metadata.series_id    = _series_id[state.sensor().index()];

    _on_stamp(our_stamp);
}

uint8_t* Guider::Decoder::allocate(unsigned size)
{
    // start() set _stamp_size from the series metadata; the SDK is
    // expected to ask for the same size for every stamp in the series.
    // If that invariant breaks, throw rather than return a wrong-sized
    // buffer (which would corrupt memory or truncate the stamp). The
    // worker thread in DaqStampSource should catch this and exit the loop.
    if (size != _stamp_size)
    {
        throw std::runtime_error(
            "Decoder::allocate: SDK requested " + std::to_string(size) +
            " bytes but series declared " + std::to_string(_stamp_size));
    }

    return _stamp_buf.data();
}
