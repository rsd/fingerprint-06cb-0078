/*
 * sigfm_geometry: the rigid motion behind SIGFM correspondences.
 *
 * For every unordered pair of images it rebuilds SIGFM's candidate matches
 * exactly as sigfm_match_score() does (SIFT from sigfm_extract() on the
 * driver's 2x nearest-neighbour upscale; BFMatcher k=2; ratio 0.75; unique
 * point pairs; stable sort by distance; cap 176), then fits a similarity
 * transform to them with RANSAC (cv::estimateAffinePartial2D, 3 px
 * reprojection threshold at 2x) and prints the inlier count, rotation and
 * translation, in native (1x) sensor pixels.
 *
 * Purpose: tell a finger match (varied translation/rotation between touches)
 * from a sensor artefact or residue match (no motion: the same pattern at the
 * same sensor position).
 *
 * Usage: sigfm_geometry IMG.pgm...   (native 144x56 P5 PGMs)
 * Output (TSV): i j candidates inliers rot_deg tx ty scale
 * Numbers only; biometric-derived, keep it out of git.
 *
 * Build: research/far-diag/build_geometry.sh FORK_BUILDDIR OUT
 *
 * SPDX-License-Identifier: MIT AND LGPL-2.1-or-later
 *
 * Parts: the Match struct and candidates() are derived from the candidate loop
 * of sigfm_match_score() in libfprint/sigfm/sigfm.cpp (SIGFM) and KEEP its terms,
 * LGPL-2.1-or-later:
 *   Copyright (C) 2022 Matthieu CHARETTE <matthieu.charette@gmail.com>
 *   Copyright (c) 2022 Natasha England-Elbro <natasha@natashaee.me>
 *   Copyright (c) 2022 Timur Mangliev <tigrmango@gmail.com>
 * The rest of this file is our own code, MIT:
 *   Copyright (C) 2026 Raul Dias <raul@dias.com.br>
 */

#include "sigfm.h"
#include "img-info.hpp"

#include <opencv2/geometry.hpp>
#include <opencv2/features2d.hpp>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <set>
#include <sstream>
#include <string>
#include <tuple>
#include <vector>

namespace {

constexpr int W = 144, H = 56, UP = 2;

bool read_pgm(const std::string& path, std::vector<unsigned char>& px)
{
    std::ifstream f(path, std::ios::binary);
    std::string magic;
    f >> magic;
    if (magic != "P5")
        return false;
    int vals[3], n = 0;
    while (n < 3 && f) {
        f >> std::ws;
        if (f.peek() == '#') {
            std::string line;
            std::getline(f, line);
            continue;
        }
        f >> vals[n++];
    }
    f.get();
    if (n != 3 || vals[0] != W || vals[1] != H || vals[2] != 255)
        return false;
    px.resize(W * H);
    f.read(reinterpret_cast<char*>(px.data()), W * H);
    return bool(f);
}

std::vector<unsigned char> upscale(const std::vector<unsigned char>& src)
{
    std::vector<unsigned char> dst(W * UP * H * UP);
    for (int y = 0; y < H * UP; y++)
        for (int x = 0; x < W * UP; x++)
            dst[y * W * UP + x] = src[(y / UP) * W + x / UP];
    return dst;
}

using Pt = cv::Point2i;
struct Match {
    Pt p1, p2;
    bool operator<(const Match& o) const
    {
        return std::tie(p1.y, p1.x, p2.y, p2.x) < std::tie(o.p1.y, o.p1.x, o.p2.y, o.p2.x);
    }
};

/* The candidate list of sigfm_match_score(frame, enrolled). */
std::vector<Match> candidates(const SigfmImgInfo* frame, const SigfmImgInfo* enrolled)
{
    std::vector<std::vector<cv::DMatch>> points;
    cv::BFMatcher::create()->knnMatch(frame->descriptors, enrolled->descriptors, points, 2);
    std::set<Match> uniq;
    std::vector<std::pair<float, Match>> c;
    for (const auto& pts : points) {
        if (pts.size() < 2)
            continue;
        if (pts[0].distance < 0.75 * pts[1].distance) {
            Match m{frame->keypoints.at(pts[0].queryIdx).pt, enrolled->keypoints.at(pts[0].trainIdx).pt};
            if (uniq.insert(m).second)
                c.emplace_back(pts[0].distance, m);
        }
    }
    std::stable_sort(c.begin(), c.end(), [](const auto& a, const auto& b) { return a.first < b.first; });
    if (c.size() > 176)
        c.resize(176);
    std::vector<Match> out;
    for (auto& x : c)
        out.push_back(x.second);
    return out;
}

} // namespace

int main(int argc, char** argv)
{
    std::vector<SigfmImgInfo*> info;
    for (int i = 1; i < argc; i++) {
        std::vector<unsigned char> px;
        if (!read_pgm(argv[i], px)) {
            std::fprintf(stderr, "%s: not a %dx%d P5 PGM\n", argv[i], W, H);
            return 2;
        }
        auto up = upscale(px);
        info.push_back(sigfm_extract(up.data(), W * UP, H * UP));
    }
    std::printf("i\tj\tcandidates\tinliers\trot_deg\ttx\tty\tscale\n");
    for (size_t i = 0; i < info.size(); i++)
        for (size_t j = i + 1; j < info.size(); j++) {
            auto m = candidates(info[i], info[j]);
            int inl = 0;
            double rot = NAN, tx = NAN, ty = NAN, sc = NAN;
            if (m.size() >= 3) {
                std::vector<cv::Point2f> a, b;
                for (auto& x : m) {
                    a.emplace_back(x.p1);
                    b.emplace_back(x.p2);
                }
                std::vector<unsigned char> mask;
                cv::Mat T = cv::estimateAffinePartial2D(a, b, mask, cv::RANSAC, 3.0, 2000, 0.99, 10);
                if (!T.empty()) {
                    for (auto v : mask)
                        inl += v;
                    double c = T.at<double>(0, 0), s = T.at<double>(1, 0);
                    rot = std::atan2(s, c) * 180.0 / M_PI;
                    sc = std::hypot(c, s);
                    tx = T.at<double>(0, 2) / UP;
                    ty = T.at<double>(1, 2) / UP;
                }
            }
            std::printf("%zu\t%zu\t%zu\t%d\t%.2f\t%.2f\t%.2f\t%.3f\n", i, j, m.size(), inl, rot, tx, ty, sc);
        }
    return 0;
}
