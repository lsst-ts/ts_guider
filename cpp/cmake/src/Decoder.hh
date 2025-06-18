#ifndef GDS_EXAMPLE_DECODER
#define GDS_EXAMPLE_DECODER

#include "dvi/TimeStamp.hh"
#include "gds/Decoder.hh"

//#include "../RoiLimits.hh"
#define MAX_ROWS 400
#define MAX_COLS 400

namespace GDS { namespace Guider {

class Decoder : public GDS::Decoder
{
public:
  Decoder(const char* partition, const GDS::LocationSet& locs);
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
  uint8_t        _stamp_buf[MAX_ROWS*MAX_COLS*sizeof(int32_t)];
};

}}
#endif
