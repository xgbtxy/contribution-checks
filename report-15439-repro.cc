// g++ -std=c++20 repro.cc -Iinclude build/librocksdb.a -lpthread -ldl -lz -lbz2 -llz4 -lzstd -lsnappy
// usage: repro DBDIR FORMAT_VERSION MODE   (MODE 0: update, 1: delete, 2: new key)
#include <rocksdb/db.h>
#include <rocksdb/table.h>
#include <rocksdb/comparator.h>
#include <iostream>
using namespace rocksdb;
#define CK(x) do { Status _s = (x); if (!_s.ok()) { std::cerr << __LINE__ << " " << _s.ToString() << "\n"; return 2; } } while (0)
int main(int argc, char** argv) {
  std::string p = argv[1]; int fv = atoi(argv[2]), mode = atoi(argv[3]);
  Options o; o.create_if_missing = true; o.comparator = ReverseBytewiseComparator();
  BlockBasedTableOptions t; t.format_version = fv;  // optimize_key_common_prefix left at its default
  o.table_factory.reset(NewBlockBasedTableFactory(t));
  DestroyDB(p, o);
  std::unique_ptr<DB> db; CK(DB::Open(o, p, &db));
  const std::string k = "user1";
  // the neighbour extends the key by the type byte of the key's own newest entry (0x01 Put, 0x00 Delete)
  const std::string neighbour = mode == 1 ? std::string("user1\x00", 6) : std::string("user1\x01", 6);
  std::string expect;
  if (mode != 2) { CK(db->Put(WriteOptions(), k, "balance=100")); CK(db->Flush(FlushOptions())); }
  if (mode == 0) { CK(db->Put(WriteOptions(), k, "balance=250")); expect = "balance=250"; }
  if (mode == 1) { CK(db->Delete(WriteOptions(), k)); expect = "<NotFound>"; }
  if (mode == 2) { CK(db->Put(WriteOptions(), k, "balance=250")); expect = "balance=250"; }
  CK(db->Put(WriteOptions(), neighbour, "another row"));  // the neighbouring key, same memtable
  CK(db->Flush(FlushOptions()));
  CK(db->Close()); db.reset(); CK(DB::Open(o, p, &db));
  std::string v; Status s = db->Get(ReadOptions(), k, &v); std::string got = s.ok() ? v : s.IsNotFound() ? "<NotFound>" : s.ToString();
  std::unique_ptr<Iterator> it(db->NewIterator(ReadOptions())); it->Seek(k);
  std::string seek = it->Valid() ? it->key().ToString() + "=" + it->value().ToString() : "<end>";
  std::string seek_exp = expect == "<NotFound>" ? "<end>" : k + "=" + expect;
  std::vector<Slice> ks{Slice(k)}; std::vector<std::string> vs; auto st = db->MultiGet(ReadOptions(), ks, &vs);
  std::string mg = st[0].ok() ? vs[0] : st[0].IsNotFound() ? "<NotFound>" : st[0].ToString();
  it.reset();
  bool ok = got == expect && mg == expect && seek == seek_exp;
  std::cout << "format_version=" << fv << "  Get=" << got << "  MultiGet=" << mg << "  Seek(user1)=" << seek << "   (expected " << expect << ")" << (ok ? "" : "   <- WRONG") << "\n";
  return ok ? 0 : 1;
}
