#ifndef GDS_EXAMPLE_SUBSCRIBER
#define GDS_EXAMPLE_SUBSCRIBER

#include "dvi/TimeStamp.hh"
#include "gds/Subscriber.hh"
#include "TimeStats.hh"

namespace GDS { namespace Guider {

class Subscriber : public GDS::Subscriber
{
public:
  Subscriber(const char* partition, const GDS::LocationSet& locs, bool verbose=false);
  void start    (const GDS::StateMetadata& state, const GDS::SeriesMetadata& series);
  void resume   (const GDS::StateMetadata& state);
  void pause    (const GDS::StateMetadata& state);
  void stop     (const GDS::StateMetadata& state);
  void raw_stamp(const GDS::StateMetadata& state, const GDS::RawStamp& stamp);
private:
  bool           _verbose;
  unsigned       _rstamp_size;
  DVI::TimeStamp _begin[GDS::Set::SIZE];
  unsigned       _raw_stamps[GDS::Set::SIZE];
  TimeStats      _latency[GDS::Set::SIZE];
};

}}

#endif
