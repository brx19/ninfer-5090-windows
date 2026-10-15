add_library(ninfer_media_acquire STATIC
  media_acquire/acquire.cpp
)
ninfer_internal_includes(ninfer_media_acquire)
if(TARGET CURL::libcurl)
  target_link_libraries(ninfer_media_acquire PRIVATE CURL::libcurl)
  target_compile_definitions(ninfer_media_acquire PRIVATE NINFER_HAVE_LIBCURL=1)
endif()
if(WIN32)
  # acquire.cpp resolves public media URLs with Winsock (WSAStartup, getaddrinfo,
  # freeaddrinfo, ntohl, inet_ntop). As a static library's private link dependency it
  # reaches every consumer's final link line, so all executables that link it resolve
  # the symbols without any global linker configuration.
  target_link_libraries(ninfer_media_acquire PRIVATE ws2_32)
endif()


add_library(ninfer_product_prompt_input STATIC
  prompt_input/prompt_input.cpp
)
ninfer_internal_includes(ninfer_product_prompt_input)
target_link_libraries(ninfer_product_prompt_input PRIVATE ninfer_media_acquire ninfer::json)

add_library(ninfer_product_logging STATIC
  logging/logging.cpp
  logging/pretty_format.cpp
  logging/startup_log.cpp
)
ninfer_internal_includes(ninfer_product_logging)
target_link_libraries(ninfer_product_logging PUBLIC spdlog::spdlog)
