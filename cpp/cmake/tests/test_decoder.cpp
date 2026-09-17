/*
 * This file is part of ts_guider.
 *
 * Developed for the Vera C. Rubin Observatory Telescope and Site Systems.
 * This product includes software developed by the LSST Project
 * (https://www.lsst.org).
 * See the COPYRIGHT file at the top-level directory of this distribution
 * for details of code ownership.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program. If not, see <https://www.gnu.org/licenses/>.
 */

#include "Decoder.hh"

#include <gtest/gtest.h>

#include <array>
#include <cstdlib>
#include <vector>

namespace {

class TestDecoder : public GDS::Guider::Decoder
{
public:
    using Decoder::Decoder;
    using Decoder::start;
    using Decoder::resume;
    using Decoder::stamp;
};

GDS::SeriesMetadata series(const GDS::Location& sensor, unsigned segment,
                           unsigned row, unsigned col, const char* id)
{
    return GDS::SeriesMetadata(
        GDS::RoiCommon(2, 3, 200), GDS::RoiLocation(sensor, segment, row, col),
        false, DVI::Version(), 0, 0, DAQ::Sensor::ITL, "test", id);
}

GDS::StateMetadata state(GDS::StateMetadata::Type type, const GDS::Location& sensor,
                        unsigned sequence, const char* comment = "")
{
    return GDS::StateMetadata(type, sequence, 1, OSA::TimeStamp(), sensor, comment);
}

}

TEST(Decoder, MetadataIsPerSensorAndStartClearsImageUntilResume)
{
    const char* partition = std::getenv("GUIDER_TEST_PARTITION");
    if (!partition)
    {
        GTEST_SKIP() << "Set GUIDER_TEST_PARTITION to a running test partition.";
    }
    std::vector<guider::StampMetadata> received;
    TestDecoder decoder(partition, GDS::LocationSet(GDS::Set::ANY),
        [&received](const guider::Stamp& stamp) {
            EXPECT_EQ(stamp.rows, 2u);
            EXPECT_EQ(stamp.cols, 3u);
            EXPECT_EQ(stamp.pixels[5], 6);
            received.push_back(stamp.metadata);
        });
    GDS::Location a(0, 1, 0);
    GDS::Location b(4, 1, 0);
    std::array<int32_t, 6> pixels{1, 2, 3, 4, 5, 6};
    GDS::Stamp stamp(reinterpret_cast<uint8_t*>(pixels.data()), sizeof(pixels));

    decoder.start(state(GDS::StateMetadata::START, a, 1), series(a, 5, 10, 20, "roi-a"));
    decoder.resume(state(GDS::StateMetadata::RESUME, a, 1, "image-a"));
    decoder.start(state(GDS::StateMetadata::START, b, 1), series(b, 17, 30, 40, "roi-b"));
    decoder.resume(state(GDS::StateMetadata::RESUME, b, 1, "image-b"));
    decoder.stamp(state(GDS::StateMetadata::STAMP, a, 1), stamp);
    decoder.stamp(state(GDS::StateMetadata::STAMP, b, 1), stamp);

    ASSERT_EQ(received.size(), 2u);
    EXPECT_EQ(received[0].segment, 5u);
    EXPECT_EQ(received[0].startrow, 10u);
    EXPECT_EQ(received[0].startcol, 20u);
    EXPECT_EQ(received[0].obs_id, "image-a");
    EXPECT_EQ(received[0].series_id, "roi-a");
    EXPECT_EQ(received[1].segment, 17u);
    EXPECT_EQ(received[1].startrow, 30u);
    EXPECT_EQ(received[1].startcol, 40u);
    EXPECT_EQ(received[1].obs_id, "image-b");
    EXPECT_EQ(received[1].series_id, "roi-b");

    // A RESUME can change images without changing the ROI series.
    decoder.resume(state(GDS::StateMetadata::RESUME, a, 1, "image-a-next"));
    decoder.stamp(state(GDS::StateMetadata::STAMP, a, 1), stamp);
    EXPECT_EQ(received.back().obs_id, "image-a-next");
    EXPECT_EQ(received.back().series_id, "roi-a");

    // A new START must not leak the preceding image before its RESUME.
    decoder.start(state(GDS::StateMetadata::START, a, 2), series(a, 7, 50, 60, "roi-a-new"));
    decoder.stamp(state(GDS::StateMetadata::STAMP, a, 2), stamp);
    EXPECT_TRUE(received.back().obs_id.empty());
    EXPECT_EQ(received.back().series_id, "roi-a-new");
    EXPECT_EQ(received.back().segment, 7u);
    EXPECT_EQ(received.back().startrow, 50u);
    EXPECT_EQ(received.back().startcol, 60u);
    decoder.stamp(state(GDS::StateMetadata::STAMP, b, 1), stamp);
    EXPECT_EQ(received.back().obs_id, "image-b");
    EXPECT_EQ(received.back().series_id, "roi-b");
}
