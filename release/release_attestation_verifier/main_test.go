package main

import (
	"bytes"
	"encoding/hex"
	"os"
	"testing"
	"time"

	"github.com/sigstore/sigstore-go/pkg/bundle"
	"github.com/sigstore/sigstore-go/pkg/fulcio/certificate"
	"github.com/sigstore/sigstore-go/pkg/root"
	"github.com/sigstore/sigstore-go/pkg/verify"
)

func TestCloseReleaseStatementRequiresExactAuthorityAndSubjects(t *testing.T) {
	request := releaseRequest{
		SchemaVersion: 1,
		Mode:          "github-release",
		Repository:    repository,
		RepositoryID:  repositoryID,
		OwnerID:       ownerID,
		Tag:           "v1.1.0-rc.1",
		TagCommit:     "1111111111111111111111111111111111111111",
		ExpectedSubjects: []expectedSubject{
			{Name: "animemo-v1.1.0-rc.1-portable.tar", SHA256: digest('a'), Size: 50},
			{Name: "checksums.txt", SHA256: digest('b'), Size: 10},
			{Name: "deployment-contract.json", SHA256: digest('c'), Size: 20},
			{Name: "installer-materials.tar", SHA256: digest('d'), Size: 30},
			{Name: "release-manifest.json", SHA256: digest('e'), Size: 40},
		},
	}
	statement := releaseStatement{
		Type:          "https://in-toto.io/Statement/v1",
		PredicateType: releasePredicateType,
		Subjects: []statementSubject{
			{Name: "pkg:github/yanyuhanyue/AniMemo@v1.1.0-rc.1", Digest: map[string]string{"sha1": request.TagCommit}},
			{Name: "animemo-v1.1.0-rc.1-portable.tar", Digest: map[string]string{"sha256": trimDigest(request.ExpectedSubjects[0].SHA256)}},
			{Name: "checksums.txt", Digest: map[string]string{"sha256": trimDigest(request.ExpectedSubjects[1].SHA256)}},
			{Name: "deployment-contract.json", Digest: map[string]string{"sha256": trimDigest(request.ExpectedSubjects[2].SHA256)}},
			{Name: "installer-materials.tar", Digest: map[string]string{"sha256": trimDigest(request.ExpectedSubjects[3].SHA256)}},
			{Name: "release-manifest.json", Digest: map[string]string{"sha256": trimDigest(request.ExpectedSubjects[4].SHA256)}},
		},
		Predicate: releasePredicate{
			OwnerID: ownerID, PackageID: repositoryID, Repository: repository,
			RepositoryID: repositoryID, Tag: request.Tag,
			PURL: "pkg:github/yanyuhanyue/AniMemo@" + request.Tag,
		},
	}

	claim, err := closeReleaseStatement(request, statement, time.Date(2026, 8, 19, 1, 2, 3, 0, time.UTC))
	if err != nil {
		t.Fatalf("expected success, got %v", err)
	}
	if len(claim.Assets) != 4 || claim.Assets[0].Name != "checksums.txt" {
		t.Fatalf("unexpected closed assets: %#v", claim.Assets)
	}
	if len(claim.TransportAssets) != 1 ||
		claim.TransportAssets[0].Name != "animemo-v1.1.0-rc.1-portable.tar" ||
		claim.TransportAssets[0].AuthorityRole != "TRANSPORT_ONLY" {
		t.Fatalf("unexpected transport asset: %#v", claim.TransportAssets)
	}

	missingPortable := statement
	missingPortable.Subjects = append(
		[]statementSubject(nil),
		statement.Subjects[:len(statement.Subjects)-1]...,
	)
	if _, err := closeReleaseStatement(request, missingPortable, time.Now()); err == nil {
		t.Fatal("missing portable subject must be rejected")
	}

	substitutedPortable := statement
	substitutedPortable.Subjects = append([]statementSubject(nil), statement.Subjects...)
	last := len(substitutedPortable.Subjects) - 1
	substitutedPortable.Subjects[last].Digest = map[string]string{"sha256": trimDigest(digest('f'))}
	if _, err := closeReleaseStatement(request, substitutedPortable, time.Now()); err == nil {
		t.Fatal("substituted portable digest must be rejected")
	}

	statement.Subjects = append(statement.Subjects, statementSubject{Name: "extra", Digest: map[string]string{"sha256": trimDigest(digest('f'))}})
	if _, err := closeReleaseStatement(request, statement, time.Now()); err == nil {
		t.Fatal("extra release subject must be rejected")
	}
}

func TestCloseReleaseStatementRejectsChangedNumericRepositoryIdentity(t *testing.T) {
	request := releaseRequest{SchemaVersion: 1, Mode: "github-release", Repository: repository, RepositoryID: repositoryID, OwnerID: ownerID}
	statement := releaseStatement{PredicateType: releasePredicateType, Predicate: releasePredicate{OwnerID: "1", PackageID: repositoryID, Repository: repository, RepositoryID: repositoryID}}
	if _, err := closeReleaseStatement(request, statement, time.Now()); err == nil {
		t.Fatal("changed owner id must be rejected")
	}
}

func TestCloseActionsStatementRequiresSignedSubjectNameAndDigest(t *testing.T) {
	request := actionsRequest{
		SchemaVersion: 1,
		Mode:          "actions-provenance",
		EvidenceName:  "release-manifest",
		Subject:       expectedSubject{Name: "release-manifest.json", SHA256: digest('a'), Size: 7},
		Workflow:      ".github/workflows/release.yml",
		SourceCommit:  "1111111111111111111111111111111111111111",
	}
	statement := actionsStatement{
		Type:          "https://in-toto.io/Statement/v1",
		PredicateType: actionsPredicateType,
		Subjects: []statementSubject{{
			Name:   "release-manifest.json",
			Digest: map[string]string{"sha256": trimDigest(request.Subject.SHA256)},
		}},
	}
	claim, err := closeActionsStatement(request, statement)
	if err != nil {
		t.Fatalf("expected success, got %v", err)
	}
	if claim.Source.Commit != request.SourceCommit || claim.Subject.SHA256 != request.Subject.SHA256 {
		t.Fatalf("unexpected closed claim: %#v", claim)
	}
	statement.Subjects[0].Name = "other.json"
	if _, err := closeActionsStatement(request, statement); err == nil {
		t.Fatal("same digest under a different file subject must be rejected")
	}
}

func TestOCISubjectMatcherRejectsLookalikeRepository(t *testing.T) {
	digestValue := digest('a')
	if !matchesOCISubject(
		"pkg:oci/animemo-api@"+digestValue+"?repository_url=ghcr.io%2Fyanyuhanyue%2Fanimemo-api",
		"ghcr.io/yanyuhanyue/animemo-api",
		digestValue,
	) {
		t.Fatal("canonical OCI purl must be accepted")
	}
	if matchesOCISubject(
		"pkg:oci/animemo-api@"+digestValue+"?repository_url=evil.example%2Fghcr.io%2Fyanyuhanyue%2Fanimemo-api",
		"ghcr.io/yanyuhanyue/animemo-api",
		digestValue,
	) {
		t.Fatal("lookalike OCI repository must be rejected")
	}
}

func TestDecodeClosedJSONRejectsDuplicateKeysAndSecondTopLevelValue(t *testing.T) {
	for _, value := range []string{
		`{"mode":"github-release","mode":"actions-provenance"}`,
		`{"mode":"github-release"} {"mode":"actions-provenance"}`,
	} {
		path := t.TempDir() + "/request.json"
		if err := os.WriteFile(path, []byte(value), 0o600); err != nil {
			t.Fatal(err)
		}
		var target map[string]any
		if err := decodeClosedJSON(path, &target); err == nil {
			t.Fatalf("ambiguous JSON must be rejected: %s", value)
		}
	}
}

func TestOfficialGitHubReleaseFixtureCryptography(t *testing.T) {
	bundlePath := os.Getenv("ANIMEMO_OFFICIAL_RELEASE_BUNDLE")
	trustedRootPath := os.Getenv("ANIMEMO_OFFICIAL_GITHUB_ROOT")
	if bundlePath == "" || trustedRootPath == "" {
		t.Skip("未提供官方 GitHub public release fixture")
	}
	rootJSON, err := os.ReadFile(trustedRootPath)
	if err != nil {
		t.Fatal(err)
	}
	entity, err := bundle.LoadJSONFromPath(bundlePath)
	if err != nil {
		t.Fatal(err)
	}
	content, err := entity.VerificationContent()
	if err != nil || content.Certificate() == nil {
		t.Fatal("official fixture certificate unavailable")
	}
	issuerOrganizations := content.Certificate().Issuer.Organization
	if len(issuerOrganizations) != 1 || issuerOrganizations[0] != releaseCertificateIssuerOrg {
		t.Fatalf("unexpected issuer organization: %#v", issuerOrganizations)
	}
	san, err := verify.NewSANMatcher("https://dotcom.releases.github.com", "")
	if err != nil {
		t.Fatal(err)
	}
	issuer, err := verify.NewIssuerMatcher("", ".*")
	if err != nil {
		t.Fatal(err)
	}
	identity, err := verify.NewCertificateIdentity(san, issuer, certificate.Extensions{})
	if err != nil {
		t.Fatal(err)
	}
	digestBytes, err := hex.DecodeString("55dbb4dc6b7edb10b48e3d7fc5bccd32318d1b55")
	if err != nil {
		t.Fatal(err)
	}
	verified := 0
	for _, line := range bytes.Split(rootJSON, []byte{'\n'}) {
		line = bytes.TrimSpace(line)
		if len(line) == 0 {
			continue
		}
		trustedRoot, err := root.NewTrustedRootFromJSON(line)
		if err != nil {
			t.Fatal(err)
		}
		verifier, err := verify.NewVerifier(trustedRoot, verify.WithSignedTimestamps(1))
		if err != nil {
			t.Fatal(err)
		}
		result, err := verifier.Verify(entity, verify.NewPolicy(
			verify.WithArtifactDigest("sha1", digestBytes),
			verify.WithCertificateIdentity(identity),
		))
		if err == nil && len(result.VerifiedTimestamps) == 1 {
			verified++
		}
	}
	if verified != 1 {
		t.Fatalf("expected exactly one GitHub root to verify, got %d", verified)
	}
}

func digest(character byte) string {
	value := make([]byte, 64)
	for i := range value {
		value[i] = character
	}
	return "sha256:" + string(value)
}
