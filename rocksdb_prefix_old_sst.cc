#include <iostream>
#include <memory>
#include <string>
#include <vector>

#include "rocksdb/comparator.h"
#include "rocksdb/db.h"
#include "rocksdb/table.h"

namespace {

using namespace ROCKSDB_NAMESPACE;

constexpr char kTarget[] = "user1";

Options MakeOptions(bool create_if_missing) {
  Options options;
  options.create_if_missing = create_if_missing;
  options.comparator = ReverseBytewiseComparator();
  BlockBasedTableOptions table_options;
  table_options.format_version = 8;
  options.table_factory.reset(NewBlockBasedTableFactory(table_options));
  return options;
}

std::string NeighborKey(const std::string& mode) {
  if (mode == "delete") {
    return std::string("user1\x00", 6);
  }
  return std::string("user1\x01", 6);
}

bool WriteOldSst(const std::string& path, const std::string& mode) {
  if (mode != "update" && mode != "delete" && mode != "new") {
    std::cerr << "unknown mode: " << mode << '\n';
    return false;
  }

  Options options = MakeOptions(true);
  Status status = DestroyDB(path, options);
  if (!status.ok()) {
    std::cerr << "DestroyDB: " << status.ToString() << '\n';
    return false;
  }

  std::unique_ptr<DB> db;
  status = DB::Open(options, path, &db);
  if (!status.ok()) {
    std::cerr << "DB::Open for writer: " << status.ToString() << '\n';
    return false;
  }

  const WriteOptions write_options;
  const std::string neighbor = NeighborKey(mode);
  if (mode == "update" || mode == "delete") {
    status = db->Put(write_options, kTarget, "balance=100");
    if (status.ok()) {
      status = db->Flush(FlushOptions());
    }
  }
  if (status.ok() && mode == "update") {
    status = db->Put(write_options, kTarget, "balance=250");
  } else if (status.ok() && mode == "delete") {
    status = db->Delete(write_options, kTarget);
  } else if (status.ok() && mode == "new") {
    status = db->Put(write_options, kTarget, "balance=250");
  }
  if (status.ok()) {
    status = db->Put(write_options, neighbor, "another row");
  }
  if (status.ok()) {
    status = db->Flush(FlushOptions());
  }
  if (status.ok()) {
    status = db->Close();
  }
  db.reset();
  if (!status.ok()) {
    std::cerr << "baseline writer failed: " << status.ToString() << '\n';
    return false;
  }
  std::cout << "OLD_SST_WRITTEN mode=" << mode << " format_version=8\n";
  return true;
}

bool ReadOldSst(const std::string& path, const std::string& mode) {
  if (mode != "update" && mode != "delete" && mode != "new") {
    std::cerr << "unknown mode: " << mode << '\n';
    return false;
  }

  Options options = MakeOptions(false);
  std::unique_ptr<DB> db;
  Status status = DB::Open(options, path, &db);
  if (!status.ok()) {
    std::cerr << "DB::Open for reader: " << status.ToString() << '\n';
    return false;
  }

  const bool expect_found = mode != "delete";
  const std::string expected = mode == "update" ? "balance=250" : "balance=250";

  std::string get_value;
  Status get_status = db->Get(ReadOptions(), kTarget, &get_value);
  const bool get_ok = expect_found
                          ? get_status.ok() && get_value == expected
                          : get_status.IsNotFound();

  std::vector<Slice> keys{Slice(kTarget)};
  std::vector<std::string> values;
  std::vector<Status> statuses = db->MultiGet(ReadOptions(), keys, &values);
  const bool multiget_ok =
      statuses.size() == 1 &&
      (expect_found ? statuses[0].ok() && values.size() == 1 &&
                          values[0] == expected
                    : statuses[0].IsNotFound());

  std::unique_ptr<Iterator> iterator(db->NewIterator(ReadOptions()));
  iterator->Seek(kTarget);
  const bool seek_is_target =
      iterator->Valid() && iterator->key().compare(Slice(kTarget)) == 0;
  const bool seek_ok = expect_found
                           ? seek_is_target && iterator->value().compare(
                                                   Slice(expected)) == 0
                           : !seek_is_target;
  const bool seek_status_ok = iterator->status().ok();

  size_t target_count = 0;
  bool scan_value_ok = true;
  iterator->SeekToFirst();
  for (; iterator->Valid(); iterator->Next()) {
    if (iterator->key().compare(Slice(kTarget)) == 0) {
      ++target_count;
      scan_value_ok = scan_value_ok && iterator->value().compare(
                                             Slice(expected)) == 0;
    }
  }
  const bool scan_ok = iterator->status().ok() &&
                       (expect_found ? target_count == 1 && scan_value_ok
                                     : target_count == 0);

  const bool ok = get_ok && multiget_ok && seek_ok && seek_status_ok && scan_ok;
  std::cout << "OLD_SST_READ mode=" << mode << " get="
            << (get_ok ? "PASS" : get_status.ToString()) << " multiget="
            << (multiget_ok ? "PASS" : "FAIL") << " seek="
            << (seek_ok && seek_status_ok ? "PASS" : "FAIL") << " scan="
            << (scan_ok ? "PASS" : "FAIL") << " result="
            << (ok ? "PASS" : "FAIL") << '\n';
  return ok;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc != 4) {
    std::cerr << "usage: rocksdb_prefix_old_sst (write|read) DB_PATH MODE\n";
    return 2;
  }
  const std::string operation = argv[1];
  const std::string path = argv[2];
  const std::string mode = argv[3];
  if (operation == "write") {
    return WriteOldSst(path, mode) ? 0 : 1;
  }
  if (operation == "read") {
    return ReadOldSst(path, mode) ? 0 : 1;
  }
  std::cerr << "unknown operation: " << operation << '\n';
  return 2;
}
