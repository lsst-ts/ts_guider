#ifndef GDS_GUIDER_DECODER_HH
#define GDS_GUIDER_DECODER_HH

#include "stamp_source.hh"

#include "dvi/TimeStamp.hh"
#include "gds/Decoder.hh"
#include "gds/LocationSet.hh"
#include "gds/Set.hh"

#include <cstdint>
#include <vector>

namespace GDS { namespace Guider {

// Wraps GDS::Decoder so that one decoded stamp callback is translated
// into a guider::Stamp (pixels + metadata) and forwarded to the
// user-supplied callback.
//
// Per Gregg (2026-05-08), GDS callbacks are serialized within a single
// Decoder instance, so a single decode buffer is safe even when this
// instance is subscribed to multiple sensors. Per-sensor counters are
// kept to mirror the SDK's Archiver example pattern.
//
// The decode buffer is allocated dynamically in start(), sized to the
// actual stamp size reported by the series metadata.
class Decoder : public GDS::Decoder
{
public:
    Decoder(const char*              partition,
            const GDS::LocationSet&  locations,
            ::guider::StampCallback  on_stamp);

private:
    void start    (const GDS::StateMetadata& state, const GDS::SeriesMetadata& series);
    void resume   (const GDS::StateMetadata& state);
    void pause    (const GDS::StateMetadata& state);
    void stop     (const GDS::StateMetadata& state);
    void raw_stamp(const GDS::StateMetadata& state, const GDS::RawStamp&       stamp);
    void stamp    (const GDS::StateMetadata& state, const GDS::Stamp&          stamp);

    uint8_t* allocate(unsigned size);

private:
    ::guider::StampCallback _on_stamp;

    // Per-series state, populated in start().
    unsigned _n_rows;
    unsigned _n_cols;
    unsigned _stamp_size;    // bytes; == _n_rows * _n_cols * sizeof(int32_t)
    unsigned _rstamp_size;   // bytes; raw stamp size from RawStamp::calc_size

    // Per-sensor bookkeeping. Sized to the SDK's Set::SIZE so any
    // sensor index returned by state.sensor().index() is valid.
    DVI::TimeStamp _begin  [GDS::Set::SIZE];
    unsigned       _stamps [GDS::Set::SIZE];
    unsigned       _rstamps[GDS::Set::SIZE];

    // Per-sensor ROI segment. 
    std::uint16_t  _segment[GDS::Set::SIZE];

    // Decode buffer handed to the SDK in allocate(). Sized once per
    // series; reused across stamps.
    std::vector<uint8_t> _stamp_buf;
};

}}

#endif
