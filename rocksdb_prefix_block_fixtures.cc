#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "db/dbformat.h"
#include "rocksdb/rocksdb_namespace.h"
#include "table/block_based/block_builder.h"

namespace ROCKSDB_NAMESPACE {

struct Fixture {
  const char* name;
  int restart_interval;
  std::vector<std::pair<std::string, std::string>> entries;
};

std::string MakeInternalKey(std::string user_key, SequenceNumber seq,
                            ValueType type) {
  AppendInternalKeyFooter(&user_key, seq, type);
  return user_key;
}

std::string BuildBlock(const Fixture& fixture) {
  BlockBuilder builder(
      fixture.restart_interval, true /* use_delta_encoding */,
      false /* use_value_delta_encoding */,
      BlockBasedTableOptions::kDataBlockBinarySearch,
      0.0 /* data_block_hash_table_util_ratio */, 0 /* ts_sz */,
      true /* persist_user_defined_timestamps */, false /* is_user_key */,
      false /* use_separated_kv_storage */, nullptr /* statistics */,
      -1.0 /* uniform_cv_threshold */, true /* use_common_prefix */);
  for (const auto& entry : fixture.entries) {
    builder.Add(Slice(entry.first), Slice(entry.second));
  }
  return builder.Finish().ToString();
}

void WriteFixture(const std::string& directory, const Fixture& fixture) {
  const std::string bytes = BuildBlock(fixture);
  const std::string path = directory + "/" + fixture.name + ".bin";
  std::ofstream output(path, std::ios::binary);
  if (!output) {
    throw std::runtime_error("cannot open fixture output: " + path);
  }
  output.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
  if (!output) {
    throw std::runtime_error("cannot write fixture output: " + path);
  }
  std::cout << fixture.name << " bytes=" << bytes.size() << " path=" << path
            << '\n';
}

}  // namespace ROCKSDB_NAMESPACE

int main(int argc, char** argv) {
  if (argc != 2) {
    std::cerr << "usage: rocksdb_prefix_block_fixtures OUTPUT_DIR\n";
    return 2;
  }

  try {
    const std::string directory = argv[1];
    const std::string empty_target =
        ROCKSDB_NAMESPACE::MakeInternalKey("", 0,
                                            ROCKSDB_NAMESPACE::kTypeValue);
    const std::vector<ROCKSDB_NAMESPACE::Fixture> fixtures = {
        {"value_tag_overlap_restart_boundary",
         2,
         {{ROCKSDB_NAMESPACE::MakeInternalKey(std::string("user1\x01z", 7),
                                              9,
                                              ROCKSDB_NAMESPACE::kTypeValue),
           "v0"},
          {ROCKSDB_NAMESPACE::MakeInternalKey(std::string("user1\x01y", 7),
                                              8,
                                              ROCKSDB_NAMESPACE::kTypeValue),
           "v1"},
          {ROCKSDB_NAMESPACE::MakeInternalKey(std::string("user1\x01", 6), 3,
                                              ROCKSDB_NAMESPACE::kTypeValue),
           "v2"},
          {ROCKSDB_NAMESPACE::MakeInternalKey("user1", 2,
                                              ROCKSDB_NAMESPACE::kTypeValue),
           "v3"}}},
        {"delete_tag_overlap",
         2,
         {{ROCKSDB_NAMESPACE::MakeInternalKey(std::string("user1\x00", 6), 3,
                                              ROCKSDB_NAMESPACE::kTypeValue),
           "v0"},
          {ROCKSDB_NAMESPACE::MakeInternalKey("user1", 2,
                                              ROCKSDB_NAMESPACE::kTypeDeletion),
           "v1"}}},
        {"empty_user_footer_overlap",
         16,
         {{ROCKSDB_NAMESPACE::MakeInternalKey(empty_target, 1,
                                              ROCKSDB_NAMESPACE::kTypeValue),
           "n0"},
          {empty_target, "v0"}}},
    };
    for (const auto& fixture : fixtures) {
      ROCKSDB_NAMESPACE::WriteFixture(directory, fixture);
    }
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
  return 0;
}
