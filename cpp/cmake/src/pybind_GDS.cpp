#include "stamp_source.hh"
#include "daq_stamp_source.hh"

#include "gds/LocationSet.hh"
#include "gds/Set.hh"

#include <pybind11/functional.h>
#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstdint>
#include <cstring>
#include <memory>
#include <string>
#include <utility>

namespace py = pybind11;

namespace {

// Holder for a Python callable invoked from the Decoder worker
// thread. When this object is destroyed, its destructor runs
// automatically and releases the Python reference safely (with the
// GIL held).
//
// Two thread/GIL concerns it solves at once:
//
//   1. operator() runs on the C++ worker thread (no GIL held by
//      definition). Acquire the GIL, build a fresh numpy array,
//      memcpy the pixels, dispatch to Python.
//
//   2. ~PythonStampCallback runs wherever the owning std::function
//      is destroyed. In practice that is DaqStampSource::unsubscribe(),
//      bound with py::call_guard<py::gil_scoped_release> - GIL is
//      *not* held when we get here. Destroying the inner py::object
//      calls dec_ref(), and pybind11 3.0 asserts GIL-held on every
//      ref-count change. Acquire the GIL before clearing the object.
//
// StampCallback is a std::function, which may copy what it stores
// internally. Copying this class is forbidden hence (= delete copy
// constructor on this class). make_python_callback() therefore puts 
// one instance on the heap inside std::shared_ptr and returns a small 
// lambda that only copies that pointer; every copy shares the same 
// Python callback, and the GIL-aware destructor runs once when the 
// last copy is gone.
class PythonStampCallback
{
public:
    explicit PythonStampCallback(py::object py_callback) :
        _py_callback(std::move(py_callback))
    {
    }

    PythonStampCallback(const PythonStampCallback&)            = delete;
    PythonStampCallback& operator=(const PythonStampCallback&) = delete;

    ~PythonStampCallback()
    {
        py::gil_scoped_acquire gil;
        _py_callback = py::object();
    }

    void operator()(const ::guider::Stamp& stamp) const
    {
        py::gil_scoped_acquire gil;  // knock on Python's door
        // GIL is held here so we can safely call into Python
        try
        {
            py::array_t<std::int32_t> pixels({
                static_cast<py::ssize_t>(stamp.rows),
                static_cast<py::ssize_t>(stamp.cols),
            });
            std::memcpy(pixels.mutable_data(),
                        stamp.pixels,
                        sizeof(std::int32_t) * stamp.rows * stamp.cols);

            _py_callback(pixels, stamp.metadata);
            
        }
        catch (const py::error_already_set& err)
        {
            py::print("python callback raised:", err.what());
        }
    } // operator scope ends, GIL is released.

private:
    py::object _py_callback;
};

::guider::StampCallback make_python_callback(py::object py_callback)
{
    auto holder = std::make_shared<PythonStampCallback>(
        std::move(py_callback));
    return [holder](const ::guider::Stamp& stamp)
    {
        (*holder)(stamp);
    };
}

}  // namespace

PYBIND11_MODULE(guiderGDS, m)
{
    m.doc() = "Guider stamp source backed by the daq-sdk (real DAQ or "
              "GDS emulator).";

    py::class_<::guider::StampMetadata>(m, "StampMetadata")
        .def_readonly("timestamp_ns", &::guider::StampMetadata::timestamp_ns)
        .def_readonly("sensor_index", &::guider::StampMetadata::sensor_index)
        .def_readonly("sequence",     &::guider::StampMetadata::sequence)
        .def_readonly("stamp_index",  &::guider::StampMetadata::stamp_index)
        .def("__repr__", [](const ::guider::StampMetadata& md)
        {
            return "<StampMetadata sensor=" + std::to_string(md.sensor_index)
                 + " seq=" + std::to_string(md.sequence)
                 + " stamp=" + std::to_string(md.stamp_index)
                 + " ts=" + std::to_string(md.timestamp_ns) + ">";
        });

    // LocationSet identifies which guide sensors a subscriber wants
    // stamps from. The GDS discovery handshake matches subscribers to
    // publishers by Location (raft/bay/board/sensor), not by raw set
    // index, so use either the SDK's location-string parser or the
    // ANY factory below.
    py::class_<GDS::LocationSet>(m, "LocationSet")
        .def(py::init<>(),
             "Construct an empty LocationSet. Pass it to "
             "DaqStampSource only if you intend to add locations "
             "another way; an empty set matches no publisher.")
        .def(py::init<const char*>(),
             py::arg("locations"),
             "Construct from the daq-sdk location string format "
             "(e.g. \"R00\", \"R00/1/0\", or a comma-separated list). "
             "This is the path the standard SDK examples use.")
        .def_static("any",
            []() { return GDS::LocationSet(GDS::Set::ANY); },
            "Return a LocationSet matching every published location. "
            "Equivalent to running gds_listener with no location "
            "arguments. Useful for catch-all subscribers in dev.")
        .def("__bool__", &GDS::LocationSet::operator bool);

    py::class_<GDS::Guider::DaqStampSource>(m, "DaqStampSource")
        .def(py::init<std::string, const GDS::LocationSet&>(),
             py::arg("partition"),
             py::arg("locations"))
        .def("start_stamp_stream",
             [](GDS::Guider::DaqStampSource& self, py::object py_callback)
             {
                 self.subscribe(make_python_callback(std::move(py_callback)));
             },
             py::arg("on_stamp"),
             "Subscribe to the GDS partition and start delivering "
             "stamps. on_stamp is called from a C++ worker thread "
             "as on_stamp(pixels, metadata).",
             py::call_guard<py::gil_scoped_release>())
        .def("stop_stamp_stream",
             &GDS::Guider::DaqStampSource::unsubscribe,
             "Stop delivering stamps, join the worker thread, and "
             "release the GDS subscription.",
             py::call_guard<py::gil_scoped_release>());
}
