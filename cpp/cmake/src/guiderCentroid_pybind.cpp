#include <string>
#include <typeinfo>
#include <iostream>

#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>

#include <guiderCentroid.h>

namespace py = pybind11;

/**
 * Type trait helper for array properties
 */
template <typename T>
struct array_trait_impl;

template <typename T, typename C, size_t N>
struct array_trait_impl<T (C::*)[N]> {
    using object_type = C;
    using value_type = T;
    static constexpr size_t size = N;
};

/**
 * Type trait for array properties
 *
 * Has the following members:
 *  - object_type, (when given a pointer to a member array) type of object
 *    (e.g. `Foo` for `&Foo::bar`)
 *  - value_type, type of array value (e.g. `int` in `int[10]`)
 *  - size, of static array (e.g. `10` in `int[10]`)
 */
template <typename T>
struct array_trait : array_trait_impl<typename std::remove_pointer<T>::type> {};

/**
 * Return a wrapper around a fixed-size array that can be bound to a
 * pybind11 property.
 *
 * @param array, a pointer to a fixed-size array member (e.g. `int[10]`)
 *
 * @returns lambda that takes a reference to an object and returns a NumPy
 * ndarray view (that shares the underlying storage). Return-value-policy
 * is set to `reference_internal` so modifications of elements change the
 * values in the underlying array.
 *
 * @note bind with e.g. `cls.def_property_readonly("bar", make_array(&Foo::bar));` (`readwrite` is not supported, but modification of array elements is).
 */
template <typename T>
auto make_array(T array) -> std::function<py::array(typename array_trait<T>::object_type const &)> {
    return [array](typename array_trait<T>::object_type const &self) {
        return py::array(array_trait<T>::size, self.*array, py::none());
    };
}


PYBIND11_MODULE(guiderCentroid,m){
    m.doc() = R"pbdoc(
         .. currentmodule:: guiderCentroid
         .. autosummary::
            :toctree:
            guiderCentroid
            guideParameters
            guiderCentroid.measureSample
            guiderCentroid.getAlgorithms
    )pbdoc";
            
    py::class_<guideParameters>(m,"guideParameters",R"pbdoc(Data strucuture for guide parameters)pbdoc" ))
        .def(py::init<>())        
        .def_readwrite( "algorithm",  &guideParameters::algorithm )
        .add_property( "roiPixels", make_array(&guideParameters::roiPixels))
        .def_property_readonly( "roiPixels", make_array(&guideParameters::roiPixels))
        .def_readwrite( "roiNumber",  &guideParameters::roiNumber )
        .def_readwrite( "stepSize",   &guideParameters::stepSize )
        .def_readwrite( "smoothing",  &guideParameters::smoothing )
        .def_readwrite( "saturation", &guideParameters::saturation )
        .def_readwrite( "xCentroid",  &guideParameters::xCentroid )
        .def_readwrite( "yCentroid",  &guideParameters::yCentroid )
        ;
        
    py::class_<guiderCentroid>(m,"guiderCentroid")
        .def(py::init<>())        
        .def( "measureSample" ,       &::guiderCentroid::measureSample )
        .def( "getAlgorithms" ,       &::guiderCentroid::getAlgorithms )
        ;

        m.attr("componentName")         = "guiderCentroid";
}


