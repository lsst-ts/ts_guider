#include <sys/time.h>
#include <stdexcept>
#include <time.h>
#include <sys/types.h>
#include <unistd.h>
#include "guiderCentroid.h"

using namespace GDS::Guider;
static GDS::Guider::Subscriber* subscriber;
const GDS::StateMetadata gdsState;
const GDS::SeriesMetadata gdsSeries;

int32_t guiderCentroid::guiderInit()
{
   for (int i=0; i<GUIDER_MAX_ROI; i++) {
     gdrPars[i].algorithm = GUIDER_ALGORITHM_CMASS;
   }
}

guiderCentroid::~guiderCentroid()
{
}

int32_t guiderCentroid::measureCentroid(uint32_t roinum)
{
   double guider_centx, guider_centy;
   uint32_t image_width, image_height;
   int32_t *pixels;
   uint32_t saturation;
   
   gdrPars[roinum].xCentroid = -1.0;
   gdrPars[roinum].yCentroid = -1.0;
   saturation = gdrPars[roinum].saturation;
    
   pixels = (int32_t *)gdrPars[roinum].guidePixels;
   
   switch (gdrPars[roinum].algorithm) {
     case GUIDER_ALGORITHM_CMASS:
      calc_cmass (pixels, roinum,saturation, &guider_centx, &guider_centy);
      break;
    case GUIDER_ALGORITHM_SHECTMAN:
      calc_plsphot (pixels, roinum,saturation, &guider_centx, &guider_centy);
      break;
     case GUIDER_ALGORITHM_CMOMENT:
      calc_cmoment (pixels, roinum, saturation, &guider_centx, &guider_centy);
      break;
     case GUIDER_ALGORITHM_QUADRANT:
      calc_quadrant (pixels, roinum, saturation, &guider_centx, &guider_centy);
      break;
     case GUIDER_ALGORITHM_GAUSSIAN:
      calc_gaussian (pixels, roinum, saturation, &guider_centx, &guider_centy);
      break;
//     case GUIDER_ALGORITHM_HSM:
//      calc_hsm (pixels, roinum, saturation, &guider_centx, &guider_centy);
//      break;
     default:
      cout << "GUIDER ERROR: Unknown centroid algorithm : " << gdrPars[roinum].algorithm << endl;
      gdrPars[roinum].lossOfSignal = true;
      break;

     if ( !gdrPars[roinum].lossOfSignal ) {
       gdrPars[roinum].xCentroid = guider_centx;
       gdrPars[roinum].yCentroid = guider_centy;
       return GUIDER_OK;
     } else {
       return GUIDER_ERROR_NOCENTROID;
     }
   }
}


uint32_t guiderCentroid::startProcessing(uint32_t pixelSource)
{
   if (pixelSource == GUIDER_PIXELS_GDS) {
      GDS::LocationSet gdsLocation;
      gdsPartition = getenv("LSSTCAM_GDS_PARTITION");
      subscriber = new GDS::Guider::Subscriber(gdsPartition, gdsLocation, false);
      subscriber->start(gdsState, gdsSeries);
   }
}

uint32_t guiderCentroid::stopProcessing(uint32_t pixelSource)
{
   if ( subscriber != NULL ) {
      subscriber->stop(gdsState);
   }
}

uint32_t guiderCentroid::startGuiding()
{
}


uint32_t guiderCentroid::stopGuiding()
{
}

