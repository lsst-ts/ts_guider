#ifndef GDS_EXAMPLE_TIMESTATS
#define GDS_EXAMPLE_TIMESTATS

namespace GDS { namespace Guider {

class TimeStats
{
public:
  TimeStats(const char* label, const char* units, double scale=1.0);
  TimeStats();
  TimeStats(const TimeStats& clone);
public:
  TimeStats& operator=(const TimeStats&);
public:
  void update(double datum);
public:
  double count() const {return _count;}
  double mean()  const {return _mean/_scale;}
  double var()   const {return (_mean/(_count-1))/_scale;}
  double max()   const {return _max/_scale;}
  double min()   const {return _min/_scale;}
public:
  const char* encode(char* buf) const;
  const char* encode() const;
private:
  char   _label[64];
  char   _units[8];
  double _scale;
  double _count;
  double _mean;
  double _m2;
  double _max;
  double _min;
};

}}

#endif
