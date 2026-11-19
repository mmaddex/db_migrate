#!/usr/bin/env perl
# Split a pg_dump --schema-only file for schema.sh.
#
#   split_schema.pl KEEP_FILE INDEXES_OUT POST_OUT < schema_full.sql > schema_no_indexes.sql
#
# * CREATE [UNIQUE] INDEX lines go to INDEXES_OUT (one statement per line, as
#   pg_dump writes them), except indexes listed in KEEP_FILE (unique indexes a
#   foreign key or Bucardo depends on), which stay in the schema.
# * Statements that need a deferred index (ALTER INDEX ... ATTACH PARTITION,
#   COMMENT ON INDEX, ALTER INDEX ... SET STATISTICS, CLUSTER ON, REPLICA
#   IDENTITY USING INDEX) go to POST_OUT, to run after the indexes exist.
# * CREATE SCHEMA x; becomes CREATE SCHEMA IF NOT EXISTS x; (public exists).
use strict; use warnings;
my ($keepfile, $idxfile, $postfile) = @ARGV;
my %keep;
open my $k, '<', $keepfile or die "keep: $!";
while (<$k>) { chomp; $keep{$_} = 1 if length }
close $k;

my $ident = qr/(?:"(?:[^"]|"")*"|[^\s."(]+)/;
my @lines = <STDIN>;

# Pass 1: which indexes are deferred (schema-qualified, as pg_dump quotes them).
my %deferred;
for (@lines) {
  if (/^CREATE (?:UNIQUE )?INDEX ($ident) ON (?:ONLY )?($ident)\./) {
    my $q = "$2.$1";
    $deferred{$q} = 1 unless $keep{$q};
  }
}
sub is_deferred { my ($q) = @_; return $deferred{$q} }

open my $idx,  '>', $idxfile  or die "idx: $!";
open my $post, '>', $postfile or die "post: $!";
my ($n_idx, $n_post) = (0, 0);
my $collect;   # multi-line statement being moved to index_post.sql
for my $line (@lines) {
  if (defined $collect) {
    $collect .= $line;
    if ($line =~ /;\s*$/) { print $post $collect; $collect = undef }
    next;
  }
  if ($line =~ /^CREATE (?:UNIQUE )?INDEX ($ident) ON (?:ONLY )?($ident)\./ && is_deferred("$2.$1")) {
    print $idx $line; $n_idx++; next;
  }
  my $dep;
  if    ($line =~ /^ALTER INDEX ($ident\.$ident) ATTACH PARTITION ($ident\.$ident);/) {
    $dep = is_deferred($1) || is_deferred($2);
  }
  elsif ($line =~ /^(?:ALTER INDEX|COMMENT ON INDEX) ($ident\.$ident)\s/) { $dep = is_deferred($1) }
  elsif ($line =~ /^ALTER TABLE (?:ONLY )?($ident)\.$ident (?:CLUSTER ON|REPLICA IDENTITY USING INDEX) ($ident);/) {
    $dep = is_deferred("$1.$2");
  }
  if ($dep) {
    $n_post++;
    if ($line =~ /;\s*$/) { print $post $line } else { $collect = $line }
    next;
  }
  # The target database already has these schemas (at least public).
  $line =~ s/^CREATE SCHEMA ($ident);/CREATE SCHEMA IF NOT EXISTS $1;/;
  print $line;
}
die "unterminated statement while splitting\n" if defined $collect;
close $idx; close $post;
print STDERR "split: $n_idx index(es) deferred, $n_post dependent statement(s), " . scalar(keys %keep) . " kept in the schema\n";
