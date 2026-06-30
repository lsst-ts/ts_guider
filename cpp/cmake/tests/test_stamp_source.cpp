// Proof-of-life GoogleTest for the guider stamp DTOs.
//
// The structs in stamp_source.hh are intentionally tiny; this test
// just exercises the public contract that the rest of the code (and
// any future consumers) relies on:
//
//   - Default-constructed instances are zero / nullptr.
//   - StampCallback can wrap a lambda and round-trip a Stamp value.
//   - The documented row-major pixel layout actually works the way
//     the comment in stamp_source.hh claims.

#include <gtest/gtest.h>

#include "stamp_source.hh"

#include <array>
#include <cstdint>

TEST(StampSource, StampMetadataDefaultsToZero)
{
    guider::StampMetadata md;

    EXPECT_EQ(md.timestamp_ns, 0u);
    EXPECT_EQ(md.sensor_index, 0u);
    EXPECT_EQ(md.sequence, 0u);
    EXPECT_EQ(md.stamp_index, 0u);
}

TEST(StampSource, StampDefaultConstructsEmpty)
{
    guider::Stamp s;

    EXPECT_EQ(s.pixels, nullptr);
    EXPECT_EQ(s.rows, 0u);
    EXPECT_EQ(s.cols, 0u);

    // metadata is a member of type StampMetadata; ensure it was
    // default-initialized too rather than left indeterminate.
    EXPECT_EQ(s.metadata.timestamp_ns, 0u);
    EXPECT_EQ(s.metadata.sensor_index, 0u);
}

TEST(StampSource, StampCallbackRoundTripsAStampValue)
{
    guider::Stamp captured;
    captured.rows = 999;  // poison so we can tell the callback ran

    guider::StampCallback cb = [&captured](const guider::Stamp& s) {
        captured = s;
    };

    guider::Stamp sent;
    sent.rows = 400;
    sent.cols = 400;
    sent.metadata.timestamp_ns = 1779413890189701790ULL;
    sent.metadata.sensor_index = 3;
    sent.metadata.sequence     = 1;
    sent.metadata.stamp_index  = 7;

    cb(sent);

    EXPECT_EQ(captured.rows, 400u);
    EXPECT_EQ(captured.cols, 400u);
    EXPECT_EQ(captured.metadata.timestamp_ns, 1779413890189701790ULL);
    EXPECT_EQ(captured.metadata.sensor_index, 3u);
    EXPECT_EQ(captured.metadata.sequence, 1u);
    EXPECT_EQ(captured.metadata.stamp_index, 7u);
}

TEST(StampSource, RowMajorPixelIndexing)
{
    // Tiny 2x3 stamp filled with sequential values so we can verify
    // the index() == row * cols + col convention documented in
    // stamp_source.hh.
    constexpr std::size_t rows = 2;
    constexpr std::size_t cols = 3;
    std::array<std::int32_t, rows * cols> buffer = {
        10, 11, 12,   // row 0
        20, 21, 22,   // row 1
    };

    guider::Stamp s;
    s.pixels = buffer.data();
    s.rows   = rows;
    s.cols   = cols;

    auto at = [&](std::size_t r, std::size_t c) {
        return s.pixels[r * s.cols + c];
    };

    EXPECT_EQ(at(0, 0), 10);
    EXPECT_EQ(at(0, 2), 12);
    EXPECT_EQ(at(1, 0), 20);
    EXPECT_EQ(at(1, 2), 22);
}
