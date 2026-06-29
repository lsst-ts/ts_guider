#ifndef TS_GUIDER_STAMP_SOURCE_HH
#define TS_GUIDER_STAMP_SOURCE_HH

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>

namespace guider {

struct StampMetadata
{
    // TAI nanoseconds (per Gregg, 2026-05-08).
    std::uint64_t timestamp_ns = 0;

    // 0..GDS::Set::SIZE-1, identifies which guide sensor produced this stamp.
    std::uint32_t sensor_index = 0;

    // Per-Start counter; constant across all stamps within one guide series.
    std::uint32_t sequence = 0;

    // Per-stamp index within the series. Identifies one acquisition; all
    // sensors producing a stamp at the same instant share the same value.
    // This is the per-frame grouping key downstream code uses to assemble
    // the combined offset.
    std::uint32_t stamp_index = 0;

    // Printable sensor name from GDS::Location::encode() (the raft / bay /
    // board / sensor string for the producing sensor).
    std::string sensor_name;

    // ROI segment (amplifier) for this sensor's series.
    std::uint16_t segment = 0;
};

struct Stamp
{
    // pixels points into the Decoder decode buffer (Stamp does not own it).
    // Layout: row 0, then row 1, ... (index row * cols + col), int32.
    // Valid only until StampCallback returns; DaqStampSource may reuse
    // the buffer for the next stamp.
    const std::int32_t* pixels = nullptr;
    std::size_t         rows   = 0;
    std::size_t         cols   = 0;
    StampMetadata       metadata;
};

using StampCallback = std::function<void(const Stamp&)>;

}  // namespace guider

#endif
