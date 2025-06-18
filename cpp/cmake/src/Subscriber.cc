
#include <stdio.h>

#include "Subscriber.hh"
#include "daq/Sensor.hh"

using namespace GDS;

Guider::Subscriber::Subscriber(const char* partition, const GDS::LocationSet& locs, bool verbose) :
  GDS::Subscriber(partition, locs),
  _verbose(verbose)
{

};

void Guider::Subscriber::start(const GDS::StateMetadata& state, const GDS::SeriesMetadata& series)
{
  state.dump(); 
  series.dump();
  stats_clear();

  _rstamp_size = RawStamp::calc_size(series);
}

void Guider::Subscriber::resume(const GDS::StateMetadata& state)
{
  state.dump();
  
  _begin [state.sensor().index()]     = state.timestamp();
  _raw_stamps[state.sensor().index()] = 0;
  _latency[state.sensor().index()]    = TimeStats("Latency", "ms", 1000000.0);
}

void Guider::Subscriber::pause(const GDS::StateMetadata& state)
{
  state.dump();
  
  uint64_t begin = (uint64_t)_begin[state.sensor().index()];
  uint64_t   end = (uint64_t)state.timestamp();
  double    diff = (end-begin)/1E9;
  
  unsigned raw_stamps = _raw_stamps[state.sensor().index()];
  double   freq       = raw_stamps/diff;
  printf("  %s:  %i raw stamps in %f sec = %f Hz\n", state.sensor().encode(), raw_stamps, diff, freq);
}

void Guider::Subscriber::stop(const GDS::StateMetadata& state)
{
  state.dump(); 
  if(_verbose) printf("  %s", _latency[state.sensor().index()].encode());
  printf("  %s: %i total stamps. Errors (xfer, size, beg, end) (%u, %u, %u, %u)\n", state.sensor().encode(), stamps(state.sensor()), err_xfer(state.sensor()), err_size(state.sensor()), err_miss_beg(state.sensor()), err_miss_end(state.sensor()));
}

void Guider::Subscriber::raw_stamp(const GDS::StateMetadata& state, const GDS::RawStamp& stamp)
{
  OSA::TimeStamp arr;

  uint64_t delta = (uint64_t)arr - (uint64_t)state.timestamp();
  _latency[state.sensor().index()].update(delta);

  ++_raw_stamps[state.sensor().index()];

  if(_rstamp_size != stamp.size()) {
    printf("ERROR - %s: Sequence %i, Stamp %i - Expected raw stamp size %u, recieved %u\n", state.sensor().encode(), state.sequence(), _raw_stamps[state.sensor().index()], _rstamp_size, stamp.size());
  }
  if(!_verbose) return;

  char buf[16];
  int64_t latency = (int64_t)(uint64_t)arr - (int64_t)(uint64_t)state.timestamp();
  printf("  Arrived: %016llx - Triggered: %016llx - Latency: %016llx - Sensor %s: Sequence %i Stamp %i %s %s %i bytes\n",
         (unsigned long long)(uint64_t)arr, (unsigned long long)(uint64_t)state.timestamp(), (long long)latency,
         state.sensor().encode(buf), state.sequence(), state.stamp(),
         GDS::StateMetadata::type(state.type()), GDS::StateMetadata::status(state.status()), stamp.size());
}

