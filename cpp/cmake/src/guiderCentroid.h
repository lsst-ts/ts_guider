#ifndef GUIDER_CENTROIDH
#define GUIDER_CENTROIDH

#include <string>
#include <vector>
#include <cassert>
#include <sstream>
#include <iostream>
#include <fstream>
#include <stdlib.h>
#include <stdio.h>
#include <time.h>
#include <sys/time.h>
#include <sys/timex.h>
#include <signal.h>
#include <unistd.h>
#include "guider.h"
#include "GDS.h"
#include "Subscriber.hh"
#include "Decoder.hh"
#include "TimeStats.hh"
#include "dvi/TimeStamp.hh"
#include "gds/Subscriber.hh"
#include "gds/LocationSet.hh"
#include "gds/RoiCommon.hh"
#include "gds/RoiLocation.hh"
#include "gds/RoiFile.hh"
#include "gds/Series.hh"
#include "gds/ClearParameters.hh"
#include "gds/ClearFile.hh"

extern void calc_centroid (void *src_buffer);
extern void calc_plsphot (void *src_buffer, int roinum, int saturation, double *centx, double *centy);
extern void calc_cmass (void *src_buffer, int roinum, int saturation,double *centx, double *centy);
extern void calc_cmoment (void *src_buffer, int roinum, int saturation,double *centx, double *centy);
extern void calc_quadrant (void *src_buffer, int roinum, int saturation,double *centx, double *centy);
extern void calc_gaussian (void *src_buffer, int roinum, int saturation,double *centx, double *centy);

using namespace std;
using namespace GDS;

#define GUIDER_PIXELS_GDS 1
#define GUIDER_PIXELS_FITS 2
#define GUIDER_PIXELS_SIMULATE 3

#define GUIDER_MAX_ROI 8
#define GUIDER_OK 1
#define GUIDER_ERROR_NOCENTROID -1



struct guiderParameters  {
  uint32_t     algorithm;
  uint32_t     roiSize;
  uint32_t     stepSize;
  uint32_t     smoothing;
  uint32_t     saturation;
  uint32_t     xCentroid;
  uint32_t     yCentroid;
  bool         lossOfSignal;
  uint32_t     *guidePixels;
};

class guiderCentroid
{
   public: 
   int32_t guiderInit();
   int32_t measureCentroid(uint32_t roiNumber);
   uint32_t startProcessing(uint32_t pixelSource);
   uint32_t stopProcessing(uint32_t pixelSource);
   uint32_t startGuiding();
   uint32_t stopGuiding();
   bool guidingActive;
   bool processingActive;
   guiderParameters gdrPars[GUIDER_MAX_ROI];
   uint32_t roiCount;

   GDS::LocationSet *gdsLocation;
   const char *gdsPartition;
//   GDS::StateMetaData *gdsState;
//   GDS::SeriesMetaData *gdsSeries;
   
   ~guiderCentroid();
};


#endif

