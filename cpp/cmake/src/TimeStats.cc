
#include <stdio.h>
#include <string.h>
#include "TimeStats.hh"

using namespace GDS::Guider;


TimeStats::TimeStats() :
_scale(1.0),
_count(0.0),
_mean(0.0),
_m2(0.0),
_max(0.0),
_min(0.0)
{
  _label[0]=0;
  _units[0]=0;
}

TimeStats::TimeStats(const char* label, const char* units, double scale) :
_label(""),
_units(""),
_scale(scale),
_count(0.0),
_mean(0.0),
_m2(0.0),
_max(0.0),
_min(0.0)
{
  strncpy(_label, label, sizeof(_label));
  strncpy(_units, units, sizeof(_units));
}

TimeStats::TimeStats(const TimeStats& clone) :
  _scale(clone._scale),
  _count(clone._count),
  _mean(clone._mean),
  _m2(clone._m2),
  _max(clone._max),
  _min(clone._min)
{
  strncpy(_label, clone._label, sizeof(_label));
  strncpy(_units, clone._units, sizeof(_units));
}

TimeStats& TimeStats::operator=(const TimeStats& clone)
{
  strncpy(_label, clone._label, sizeof(_label));
  strncpy(_units, clone._units, sizeof(_units));
  _scale = clone._scale;
  _count = clone._count;
  _mean  = clone._mean;
  _m2    = clone._m2;
  _max   = clone._max;
  _min   = clone._min;
  return *this;
}

void TimeStats::update(double datum)
{
  ++_count;
  double delta = datum - _mean;
  _mean += delta/_count;
  double delta2 = datum - _mean;
  _m2 += delta * delta2;

  _min = datum<_min ? datum : _min;
  _min = _min==0.0 ? datum : _min;
  _max = datum>_max ? datum : _max;
}

const char* TimeStats::encode() const
{
  static char buf[256];
  return encode(buf);
}

const char* TimeStats::encode(char* buf) const
{
  sprintf(buf, "%s: count %u, mean %f %s, var %f %s, min %f %s, max %f %s\n", _label, (unsigned)_count, mean(), _units, var(), _units, min(), _units, max(), _units);
  return buf;
}

