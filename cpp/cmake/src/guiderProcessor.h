#ifndef GUIDER_PROCESSOR
#define GUIDER_PROCESSOR

#include "dvi/TimeStamp.hh"
#include "gds/Decoder.hh"
//#include "TimeStats.hh"
//#include "guiderProcessor.hh"

//#include "../RoiLimits.hh"


#include "guiderCentroid.h"
#include "guiderProcessor.h"
#include "GDS.h"


namespace GDS { namespace guider {

class guiderProcessor : public GDS::Decoder
{
public:
  guiderProcessor(const char* partition, const GDS::LocationSet& locs);
  guiderParameters *gdsROIS;
private:
  void start    (const GDS::StateMetadata& state, const GDS::SeriesMetadata& series);
  void resume   (const GDS::StateMetadata& state);
  void pause    (const GDS::StateMetadata& state);
  void stop     (const GDS::StateMetadata& state);
  void raw_stamp(const GDS::StateMetadata& state, const GDS::RawStamp& stamp);
private:
  void stamp(const GDS::StateMetadata& state, const GDS::Stamp& stamp);
private:
  uint8_t* allocate(unsigned size);
private:
  unsigned       _rstamp_size;
  unsigned       _stamp_size;
  DVI::TimeStamp _begin[GDS::Set::SIZE];
  unsigned       _stamps[GDS::Set::SIZE];
  unsigned       _rstamps[GDS::Set::SIZE];
//  uint8_t        _stamp_buf[RoiLimits::MAX_ROWS*RoiLimits::MAX_COLS*sizeof(int32_t)];
};
}}
#endif
