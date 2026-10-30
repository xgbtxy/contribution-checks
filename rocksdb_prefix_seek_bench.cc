//  Copyright (c) Meta Platforms, Inc. and affiliates.
//  This source code is licensed under both the GPLv2 (found in the
//  COPYING file in the root directory) and Apache 2.0 License
//  (found in the LICENSE.Apache file in the root directory).

// Small in-memory read-path benchmark for meta/rocksdb#15439. It constructs
// format_version=8 data blocks with the upstream BlockBuilder, then measures
// upstream Block/DataBlockIter seeks. The source revision, product diff, and
// static library digest are required caller inputs so results can be tied to
// the exact build artifacts by the runner.

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "db/dbformat.h"
#include "rocksdb/comparator.h"
#include "rocksdb/slice.h"
#include "table/block_based/block.h"
#include "table/block_based/block_builder.h"
#include "table/format.h"
#include "util/coding.h"

namespace ROCKSDB_NAMESPACE {
namespace {

constexpr size_t kNumKeys = 128;
constexpr size_t kPrefixSize = 64;
constexpr int kRestartInterval = 16;
constexpr size_t kWarmupIterations = 10000;
constexpr size_t kDefaultIterations = 200000;
constexpr size_t kMaxIterations = 1000000;
constexpr size_t kMeasuredRepeats = 3;

struct Entry {
  std::string user_key;
  std::string internal_key;
  std::string value;
};

struct Query {
  std::string target_user_key;
  std::string target_internal_key;
  size_t expected_index;
};

struct Workload {
  const char *name;
  std::vector<Query> queries;
};

std::string PaddedNumber(size_t number) {
  std::ostringstream out;
  out << std::setw(8) << std::setfill('0') << number;
  return out.str();
}

std::string MakeInternalKey(const std::string &user_key,
                            SequenceNumber sequence_number) {
  std::string internal_key = user_key;
  AppendInternalKeyFooter(&internal_key, sequence_number, kTypeValue);
  return internal_key;
}

bool IsHexString(const std::string &value, size_t expected_length) {
  return value.size() == expected_length &&
         std::all_of(value.begin(), value.end(),
                     [](unsigned char c) { return std::isxdigit(c) != 0; });
}

size_t ParseIterations(const char *input) {
  char *end = nullptr;
  const unsigned long long value = std::strtoull(input, &end, 10);
  if (end == input || *end != '\0' || value < 1000 || value > kMaxIterations) {
    return 0;
  }
  return static_cast<size_t>(value);
}

const char *TargetPlatform() {
#if defined(__linux__) && defined(__x86_64__)
  return "linux-x86_64";
#elif defined(__linux__) && defined(__aarch64__)
  return "linux-aarch64";
#elif defined(__APPLE__) && defined(__aarch64__)
  return "macos-aarch64";
#elif defined(__APPLE__) && defined(__x86_64__)
  return "macos-x86_64";
#elif defined(_WIN32) && defined(_M_X64)
  return "windows-x86_64";
#else
  return "unknown-target";
#endif
}

class PreparedBlock {
public:
  PreparedBlock(const Comparator *comparator, bool use_common_prefix)
      : comparator_(comparator), use_common_prefix_(use_common_prefix),
        comparator_name_(comparator == ReverseBytewiseComparator()
                             ? "reverse_bytewise"
                             : "bytewise") {
    entries_.reserve(kNumKeys);
    const std::string prefix(kPrefixSize, 'p');
    for (size_t i = 0; i < kNumKeys; ++i) {
      Entry entry;
      entry.user_key = prefix + PaddedNumber(i);
      entry.internal_key = MakeInternalKey(entry.user_key, 1);
      entry.value = "value_" + PaddedNumber(i);
      entries_.push_back(std::move(entry));
    }

    const InternalKeyComparator internal_comparator(comparator_);
    std::sort(entries_.begin(), entries_.end(),
              [&internal_comparator](const Entry &left, const Entry &right) {
                return internal_comparator.Compare(left.internal_key,
                                                   right.internal_key) < 0;
              });

    BlockBuilder builder(
        kRestartInterval, true /* use_delta_encoding */,
        false /* use_value_delta_encoding */,
        BlockBasedTableOptions::kDataBlockBinarySearch,
        0.75 /* data_block_hash_table_util_ratio */, 0 /* ts_sz */,
        true /* persist_user_defined_timestamps */, false /* is_user_key */,
        false /* use_separated_kv_storage */, nullptr /* statistics */,
        -1.0 /* uniform_cv_threshold */, use_common_prefix_);
    for (const Entry &entry : entries_) {
      builder.Add(entry.internal_key, entry.value);
    }
    const Slice built = builder.Finish();
    block_bytes_.assign(built.data(), built.size());

    BlockContents contents;
    contents.data = Slice(block_bytes_);
    block_ = std::make_unique<Block>(
        std::move(contents), 0 /* read_amp_bytes_per_bit */,
        nullptr /* statistics */, kRestartInterval);
    iter_.reset(
        block_->NewDataIterator(comparator_, kDisableGlobalSequenceNumber));
    if (!iter_->status().ok()) {
      throw std::runtime_error("constructed data block is corrupt: " +
                               iter_->status().ToString());
    }
    if (block_->NumRestarts() == 0) {
      throw std::runtime_error("constructed data block has no restart points");
    }
    const size_t footer_size = (block_->NumRestarts() + 1) * sizeof(uint32_t);
    if (block_bytes_.size() < footer_size) {
      throw std::runtime_error("constructed data block has a truncated footer");
    }
    const size_t first_restart_offset =
        DecodeFixed32(block_bytes_.data() + block_bytes_.size() - footer_size);
    if ((first_restart_offset != 0) != use_common_prefix_) {
      throw std::runtime_error(
          "common-prefix fixture mode did not match block");
    }
    common_prefix_bytes_ = first_restart_offset;
  }

  const char *ComparatorName() const { return comparator_name_; }
  bool UsesCommonPrefix() const { return use_common_prefix_; }
  size_t CommonPrefixBytes() const { return common_prefix_bytes_; }
  const std::vector<Entry> &entries() const { return entries_; }
  DataBlockIter *iter() const { return iter_.get(); }
  size_t block_size() const { return block_bytes_.size(); }

private:
  const Comparator *comparator_;
  const bool use_common_prefix_;
  const char *comparator_name_;
  std::vector<Entry> entries_;
  std::string block_bytes_;
  size_t common_prefix_bytes_ = 0;
  std::unique_ptr<Block> block_;
  std::unique_ptr<DataBlockIter> iter_;
};

size_t FindExpectedIndex(const InternalKeyComparator &comparator,
                         const std::vector<Entry> &entries,
                         const std::string &target_internal_key) {
  for (size_t i = 0; i < entries.size(); ++i) {
    if (comparator.Compare(entries[i].internal_key, target_internal_key) >= 0) {
      return i;
    }
  }
  return entries.size();
}

Query MakeQuery(const InternalKeyComparator &comparator,
                const std::vector<Entry> &entries,
                std::string target_user_key) {
  Query query;
  query.target_user_key = std::move(target_user_key);
  query.target_internal_key =
      MakeInternalKey(query.target_user_key, kMaxSequenceNumber);
  query.expected_index =
      FindExpectedIndex(comparator, entries, query.target_internal_key);
  return query;
}

std::vector<Workload> MakeWorkloads(const Comparator *comparator,
                                    const std::vector<Entry> &entries) {
  const InternalKeyComparator internal_comparator(comparator);
  std::vector<Workload> workloads;
  workloads.reserve(4);

  Workload existing{"matching_existing", {}};
  Workload gaps{"matching_gap", {}};
  Workload before{"mismatch_before_range", {}};
  Workload after{"mismatch_after_range", {}};
  existing.queries.reserve(kNumKeys);
  gaps.queries.reserve(kNumKeys);
  before.queries.reserve(kNumKeys);
  after.queries.reserve(kNumKeys);

  const std::string prefix(kPrefixSize, 'p');
  for (size_t i = 0; i < kNumKeys; ++i) {
    existing.queries.push_back(
        MakeQuery(internal_comparator, entries, prefix + PaddedNumber(i)));
    gaps.queries.push_back(MakeQuery(internal_comparator, entries,
                                     prefix + PaddedNumber(i) + "x"));

    // Bytewise order puts 'a' before and 'z' after the 'p' key range. Reverse
    // bytewise order swaps those positions.
    const bool reverse = comparator == ReverseBytewiseComparator();
    before.queries.push_back(
        MakeQuery(internal_comparator, entries,
                  std::string(reverse ? "z" : "a") + PaddedNumber(i)));
    after.queries.push_back(
        MakeQuery(internal_comparator, entries,
                  std::string(reverse ? "a" : "z") + PaddedNumber(i)));
  }
  workloads.push_back(std::move(existing));
  workloads.push_back(std::move(gaps));
  workloads.push_back(std::move(before));
  workloads.push_back(std::move(after));
  return workloads;
}

bool ValidateQuery(PreparedBlock *block, const Query &query,
                   std::string *error) {
  DataBlockIter *iter = block->iter();
  iter->Seek(query.target_internal_key);
  if (!iter->status().ok()) {
    *error = "seek returned status " + iter->status().ToString();
    return false;
  }
  const std::vector<Entry> &entries = block->entries();
  if (query.expected_index == entries.size()) {
    if (iter->Valid()) {
      *error = "seek expected end but returned " +
               ExtractUserKey(iter->key()).ToString();
      return false;
    }
    return true;
  }
  if (!iter->Valid()) {
    *error = "seek unexpectedly returned end";
    return false;
  }
  const Entry &expected = entries[query.expected_index];
  if (ExtractUserKey(iter->key()).ToString() != expected.user_key ||
      iter->value().ToString() != expected.value) {
    *error = "seek returned a key/value different from the linear oracle";
    return false;
  }
  return true;
}

std::uint64_t RunBatch(PreparedBlock *block, const std::vector<Query> &queries,
                       size_t iterations) {
  DataBlockIter *iter = block->iter();
  std::uint64_t checksum = 0;
  const size_t query_mask = queries.size() - 1;
  for (size_t i = 0; i < iterations; ++i) {
    const Query &query = queries[i & query_mask];
    iter->Seek(query.target_internal_key);
    if (iter->Valid()) {
      const Slice key = iter->key();
      const Slice value = iter->value();
      checksum = checksum * 131 + key.size() + value.size();
      if (!value.empty()) {
        checksum += static_cast<unsigned char>(value[0]);
      }
    } else {
      checksum = checksum * 131 + 1;
    }
  }
  return checksum;
}

void PrintBenchmark(PreparedBlock *block, const Workload &workload,
                    size_t iterations) {
  std::string validation_error;
  for (const Query &query : workload.queries) {
    if (!ValidateQuery(block, query, &validation_error)) {
      throw std::runtime_error(
          std::string(block->ComparatorName()) +
          " prefix=" + (block->UsesCommonPrefix() ? "on" : "off") +
          " workload=" + workload.name + ": " + validation_error);
    }
  }

  std::uint64_t checksum = RunBatch(block, workload.queries,
                                    std::min(kWarmupIterations, iterations));
  std::vector<double> ns_per_seek;
  ns_per_seek.reserve(kMeasuredRepeats);
  for (size_t repeat = 0; repeat < kMeasuredRepeats; ++repeat) {
    const auto start = std::chrono::steady_clock::now();
    checksum ^= RunBatch(block, workload.queries, iterations);
    const auto finish = std::chrono::steady_clock::now();
    const double elapsed_ns =
        std::chrono::duration<double, std::nano>(finish - start).count();
    ns_per_seek.push_back(elapsed_ns / static_cast<double>(iterations));
  }
  std::sort(ns_per_seek.begin(), ns_per_seek.end());
  std::cout << block->ComparatorName() << ','
            << (block->UsesCommonPrefix() ? "on" : "off") << ','
            << workload.name << ',' << block->CommonPrefixBytes() << ','
            << block->block_size() << ',' << iterations << ','
            << kMeasuredRepeats << ',' << std::fixed << std::setprecision(2)
            << ns_per_seek.front() << ',' << ns_per_seek[ns_per_seek.size() / 2]
            << ',' << ns_per_seek.back() << ',' << checksum << '\n';
}

} // namespace
} // namespace ROCKSDB_NAMESPACE

int main(int argc, char **argv) {
  using namespace ROCKSDB_NAMESPACE;
  if (argc < 4 || argc > 5) {
    std::cerr << "usage: " << argv[0]
              << " <upstream-source-sha40> <product-diff-sha256|none>"
                 " <librocksdb-sha256> [iterations]\n";
    return 2;
  }

  const std::string source_sha = argv[1];
  const std::string product_diff_sha = argv[2];
  const std::string library_sha = argv[3];
  if (!IsHexString(source_sha, 40) ||
      (product_diff_sha != "none" && !IsHexString(product_diff_sha, 64)) ||
      !IsHexString(library_sha, 64)) {
    std::cerr
        << "source SHA, product diff SHA, or static-library SHA is invalid\n";
    return 2;
  }
  const size_t iterations =
      argc == 5 ? ParseIterations(argv[4]) : kDefaultIterations;
  if (iterations == 0) {
    std::cerr << "iterations must be between 1000 and " << kMaxIterations
              << '\n';
    return 2;
  }

  try {
    std::cout << "input_source_git_sha=" << source_sha << '\n'
              << "input_product_diff_sha256=" << product_diff_sha << '\n'
              << "input_librocksdb_sha256=" << library_sha << '\n'
              << "target_platform=" << TargetPlatform() << '\n';
#if defined(__VERSION__)
    std::cout << "compiler=" << __VERSION__ << '\n';
#endif
    std::cout << "format_version=8\nentries=" << kNumKeys
              << "\nuser_key_prefix_bytes=" << kPrefixSize
              << "\nrestart_interval=" << kRestartInterval
              << "\niterations_per_repeat=" << iterations
              << "\nmeasured_repeats=" << kMeasuredRepeats
              << "\ncomparator,prefix_mode,workload,prefix_bytes,block_bytes,"
                 "iterations,repeats,ns_per_seek_min,"
                 "ns_per_seek_median,ns_per_seek_max,checksum\n";

    for (const Comparator *comparator :
         {BytewiseComparator(), ReverseBytewiseComparator()}) {
      for (bool use_common_prefix : {false, true}) {
        PreparedBlock block(comparator, use_common_prefix);
        const std::vector<Workload> workloads =
            MakeWorkloads(comparator, block.entries());
        for (const Workload &workload : workloads) {
          PrintBenchmark(&block, workload, iterations);
        }
      }
    }
  } catch (const std::exception &error) {
    std::cerr << "benchmark setup or correctness validation failed: "
              << error.what() << '\n';
    return 1;
  }
  return 0;
}
