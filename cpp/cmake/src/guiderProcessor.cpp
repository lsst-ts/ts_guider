#include "daq/Sensor.hh"

#include "guiderProcessor.hh"

using namespace GDS;

guiderProcessor::Decoder(const char* partition, const GDS::LocationSet& locs) : 
  GDS::Decoder(partition, locs)
{
  guiderCentroid::guiderCentroid gdsROIS = new guiderCentroid::guiderCentroid();
};

void guiderProcessor::start  (const GDS::StateMetadata& state, const GDS::SeriesMetadata& series)
{
  state.dump(); 
  series.dump();

  _stamp_size  = series.common().pixels()*sizeof(int32_t);
  _rstamp_size = RawStamp::calc_size(series);
}

void guiderProcessor::resume (const GDS::StateMetadata& state)
{
  state.dump();
  
  _begin [state.sensor().index()]        = state.timestamp();
  _stamps[state.sensor().index()]        = 0;
  _rstamps[state.sensor().index()]       = 0;
}

void guiderProcessor::pause  (const GDS::StateMetadata& state)
{
  state.dump();
  
  uint64_t begin = (uint64_t)_begin[state.sensor().index()];
  uint64_t   end = (uint64_t)state.timestamp();
  double    diff = (end-begin)/1E9;
  
  unsigned stamps        = _stamps[state.sensor().index()];
  unsigned rstamps       = _rstamps[state.sensor().index()];
  double   freq          = rstamps/diff;
  printf("  %s: (%i) %i (raw) stamps in %f sec = %f Hz\n", state.sensor().encode(), rstamps, stamps, diff, freq);

}

void guiderProcessor::stop   (const GDS::StateMetadata& state)
{
  state.dump(); 

  unsigned stamps        = _stamps[state.sensor().index()];
  unsigned rstamps       = _rstamps[state.sensor().index()];
  printf("  %s: (%i) %i (raw) stamps. Errors (xfer, size, beg, end) (%u, %u, %u, %u)\n", state.sensor().encode(), rstamps, stamps, err_xfer(state.sensor()), err_size(state.sensor()), err_miss_beg(state.sensor()), err_miss_end(state.sensor()));
}

void guiderProcessor::raw_stamp(const GDS::StateMetadata& state, const GDS::RawStamp& stamp)
{
  ++_rstamps[state.sensor().index()];

  if(_rstamp_size != stamp.size()) {
    printf("ERROR - %s: Sequence %i, Stamp %i - Expected raw stamp size %u, recieved %u.\n", state.sensor().encode(), state.sequence(), _rstamps[state.sensor().index()], _rstamp_size, stamp.size());
  }
}

void guiderProcessor::stamp(const GDS::StateMetadata& state, const GDS::Stamp& stamp)
{
  ++_stamps[state.sensor().index()];

  if(_stamp_size != stamp.size()) {
    printf("ERROR - %s: Sequence %i, Stamp %i - Expected stamp size %u, recieved %u\n", state.sensor().encode(), state.sequence(), _stamps[state.sensor().index()], _stamp_size, stamp.size());
  } else {
    gdsROIS.gdsPars[_stamps[state.sensor().index()].roiSize = (int)sqrt(stamp.size());
    gdsROIS.measureCentroid(_stamps[state.sensor().index());
  }
}

uint8_t* guiderProcessor::allocate(unsigned size)
{
  return _stamp_buf;
}

