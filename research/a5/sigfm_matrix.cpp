/*
 * sigfm_matrix: full ordered SIGFM score matrix over a list of PGM images,
 * computed with the fork's compiled SIGFM (libfprint/sigfm), so the numbers
 * are the ones the synaptics0078 driver path produces.
 *
 * Mirrors the driver and core exactly:
 *   - the image is upscaled by --scale with nearest-neighbour pixel
 *     replication, as build_image() in libfprint/drivers/synaptics0078.c does
 *     (dst[y][x] = src[y / scale][x / scale]); no other preprocessing;
 *   - sigfm_extract() on that buffer, then the keypoint gate of
 *     fp_image_sigfm_extract_thread_func() in libfprint/fp-image.c: a NULL
 *     result or fewer than 25 keypoints is rejected (the device would ask for
 *     the finger again);
 *   - score[p][g] = sigfm_match_score(info[g], info[p]): the template stage is
 *     the k-NN query and the probe the train set, the argument order of
 *     fpi_print_sigfm_match() in libfprint/fpi-print.c. Row = probe,
 *     column = enrolled image.
 *
 * Also measured: wall time of each extraction and each comparison, and the
 * size of sigfm_serialize_binary() per image (one enrolled stage of a stored
 * FpPrint). With --roundtrip every comparison is repeated against the
 * serialize -> deserialize copy of the template, as fprintd loads it from
 * disk, and any score difference is counted.
 *
 * OpenCV threading follows OPENCV_FOR_THREADS_NUM (unset: all cores, as in
 * fprintd).
 *
 * Output: one JSON object on stdout (or --out FILE). It identifies images by
 * their path only; the output is biometric-derived, keep it out of git.
 *
 * Build: research/a5/build_harness.sh (links the fork's libsigfm.a and the
 * OpenCV it was built against).
 */

#include "sigfm.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

namespace {

constexpr int min_keypoints = 25; /* fp-image.c: "No enough keypoints found" */

struct Image {
    int w = 0, h = 0;
    std::vector<unsigned char> px;
};

/* Binary (P5) 8-bit PGM, comment lines allowed in the header. */
bool read_pgm(const std::string& path, Image& im, std::string& err)
{
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        err = "cannot open";
        return false;
    }
    std::string magic;
    f >> magic;
    if (magic != "P5") {
        err = "not a binary PGM";
        return false;
    }
    auto next_int = [&f]() {
        int v = -1;
        while (f) {
            int c = f.peek();
            if (c == '#') {
                std::string line;
                std::getline(f, line);
                continue;
            }
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                f.get();
                continue;
            }
            f >> v;
            break;
        }
        return v;
    };
    const int w = next_int(), h = next_int(), maxv = next_int();
    if (w <= 0 || h <= 0 || maxv != 255) {
        err = "bad PGM header";
        return false;
    }
    f.get(); /* the single whitespace after maxval */
    im.w = w;
    im.h = h;
    im.px.resize(static_cast<size_t>(w) * h);
    f.read(reinterpret_cast<char*>(im.px.data()), static_cast<std::streamsize>(im.px.size()));
    if (!f) {
        err = "short PGM payload";
        return false;
    }
    return true;
}

/* Same loop as build_image() in synaptics0078.c. */
Image upscale(const Image& src, int scale)
{
    Image out;
    out.w = src.w * scale;
    out.h = src.h * scale;
    out.px.resize(static_cast<size_t>(out.w) * out.h);
    for (int y = 0; y < out.h; y++) {
        const unsigned char* s = src.px.data() + static_cast<size_t>(y / scale) * src.w;
        unsigned char* d = out.px.data() + static_cast<size_t>(y) * out.w;
        for (int x = 0; x < out.w; x++)
            d[x] = s[x / scale];
    }
    return out;
}

double ms_since(std::chrono::steady_clock::time_point t0)
{
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
}

std::string json_str(const std::string& s)
{
    std::string o = "\"";
    for (char c : s) {
        if (c == '"' || c == '\\') {
            o += '\\';
            o += c;
        } else if (static_cast<unsigned char>(c) < 0x20) {
            char buf[8];
            std::snprintf(buf, sizeof buf, "\\u%04x", c);
            o += buf;
        } else {
            o += c;
        }
    }
    return o + "\"";
}

void usage()
{
    std::fprintf(stderr,
                 "usage: sigfm_matrix [--scale N] [--roundtrip] [--out FILE] IMAGE.pgm...\n"
                 "  --scale N     nearest-neighbour upscale factor, as the driver (default 1)\n"
                 "  --roundtrip   also score against serialize->deserialize copies\n"
                 "  --out FILE    write the JSON here instead of stdout\n");
}

struct Entry {
    std::string path;
    int width = 0, height = 0;
    SigfmImgInfo* info = nullptr;
    SigfmImgInfo* loaded = nullptr; /* deserialized copy, --roundtrip */
    int keypoints = -1;
    bool rejected = false;
    std::string reason;
    double extract_ms = 0;
    int serialized_bytes = 0;
};

} // namespace

int main(int argc, char** argv)
{
    int scale = 1;
    bool roundtrip = false;
    std::string out_path;
    std::vector<std::string> paths;

    for (int i = 1; i < argc; i++) {
        const std::string a = argv[i];
        if (a == "--scale" && i + 1 < argc) {
            scale = std::atoi(argv[++i]);
        } else if (a == "--roundtrip") {
            roundtrip = true;
        } else if (a == "--out" && i + 1 < argc) {
            out_path = argv[++i];
        } else if (a == "-h" || a == "--help") {
            usage();
            return 0;
        } else if (!a.empty() && a[0] == '-') {
            usage();
            return 2;
        } else {
            paths.push_back(a);
        }
    }
    if (paths.empty() || scale < 1 || scale > 8) {
        usage();
        return 2;
    }

    std::vector<Entry> entries(paths.size());
    for (size_t i = 0; i < paths.size(); i++) {
        Entry& e = entries[i];
        e.path = paths[i];
        Image im;
        std::string err;
        if (!read_pgm(e.path, im, err)) {
            std::fprintf(stderr, "%s: %s\n", e.path.c_str(), err.c_str());
            return 1;
        }
        const Image up = upscale(im, scale);
        e.width = up.w;
        e.height = up.h;
        const auto t0 = std::chrono::steady_clock::now();
        e.info = sigfm_extract(up.px.data(), up.w, up.h);
        e.extract_ms = ms_since(t0);
        if (!e.info) {
            e.rejected = true;
            e.reason = "sigfm_extract failed";
            continue;
        }
        e.keypoints = sigfm_keypoints_count(e.info);
        int len = 0;
        unsigned char* blob = sigfm_serialize_binary(e.info, &len);
        e.serialized_bytes = len;
        if (roundtrip && blob)
            e.loaded = sigfm_deserialize_binary(blob, len);
        std::free(blob);
        if (e.keypoints < min_keypoints) {
            e.rejected = true;
            e.reason = "fewer than 25 keypoints";
        }
    }

    const size_t n = entries.size();
    std::vector<std::vector<long long>> score(n, std::vector<long long>(n, -2));
    std::vector<std::vector<double>> match_ms(n, std::vector<double>(n, -1));
    int roundtrip_checked = 0, roundtrip_diff = 0;

    for (size_t p = 0; p < n; p++) {
        if (entries[p].rejected)
            continue;
        for (size_t g = 0; g < n; g++) {
            if (entries[g].rejected)
                continue;
            const auto t0 = std::chrono::steady_clock::now();
            const int s = sigfm_match_score(entries[g].info, entries[p].info);
            match_ms[p][g] = ms_since(t0);
            score[p][g] = s;
            if (roundtrip) {
                roundtrip_checked++;
                if (!entries[g].loaded ||
                    sigfm_match_score(entries[g].loaded, entries[p].info) != s)
                    roundtrip_diff++;
            }
        }
    }

    std::ostringstream o;
    o << "{\n \"tool\": \"sigfm_matrix\",\n \"scale\": " << scale
      << ",\n \"min_keypoints\": " << min_keypoints
      << ",\n \"score_convention\": \"score[p][g] = sigfm_match_score(info[g], info[p]); row = probe, column = "
         "enrolled; null = an image was rejected\""
      << ",\n \"opencv_threads_env\": "
      << json_str(std::getenv("OPENCV_FOR_THREADS_NUM") ? std::getenv("OPENCV_FOR_THREADS_NUM") : "")
      << ",\n \"roundtrip\": {\"checked\": " << roundtrip_checked << ", \"differ\": " << roundtrip_diff << "}"
      << ",\n \"images\": [\n";
    for (size_t i = 0; i < n; i++) {
        const Entry& e = entries[i];
        o << "  {\"path\": " << json_str(e.path) << ", \"width\": " << e.width << ", \"height\": " << e.height
          << ", \"keypoints\": " << e.keypoints << ", \"rejected\": " << (e.rejected ? "true" : "false")
          << ", \"reason\": " << json_str(e.reason) << ", \"extract_ms\": " << e.extract_ms
          << ", \"serialized_bytes\": " << e.serialized_bytes << "}" << (i + 1 < n ? "," : "") << "\n";
    }
    o << " ],\n \"scores\": [\n";
    for (size_t p = 0; p < n; p++) {
        o << "  [";
        for (size_t g = 0; g < n; g++) {
            if (score[p][g] == -2)
                o << "null";
            else
                o << score[p][g];
            o << (g + 1 < n ? ", " : "");
        }
        o << "]" << (p + 1 < n ? "," : "") << "\n";
    }
    o << " ],\n \"match_ms\": [\n";
    for (size_t p = 0; p < n; p++) {
        o << "  [";
        for (size_t g = 0; g < n; g++) {
            if (match_ms[p][g] < 0)
                o << "null";
            else
                o << match_ms[p][g];
            o << (g + 1 < n ? ", " : "");
        }
        o << "]" << (p + 1 < n ? "," : "") << "\n";
    }
    o << " ]\n}\n";

    for (Entry& e : entries) {
        if (e.info)
            sigfm_free_info(e.info);
        if (e.loaded)
            sigfm_free_info(e.loaded);
    }

    if (out_path.empty()) {
        std::fputs(o.str().c_str(), stdout);
    } else {
        std::ofstream f(out_path);
        f << o.str();
        if (!f) {
            std::fprintf(stderr, "%s: write failed\n", out_path.c_str());
            return 1;
        }
    }
    return roundtrip_diff ? 3 : 0;
}
