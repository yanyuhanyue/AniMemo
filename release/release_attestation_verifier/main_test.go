package main

import (
	"bytes"
	"crypto"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/sigstore/sigstore-go/pkg/bundle"
	"github.com/sigstore/sigstore-go/pkg/fulcio/certificate"
	"github.com/sigstore/sigstore-go/pkg/root"
	"github.com/sigstore/sigstore-go/pkg/verify"
	"github.com/sigstore/sigstore/pkg/signature"
	"github.com/theupdateframework/go-tuf/v2/metadata"
)

const publicActionsBundleName = "sha256-2588108838c23c9b7e29d70d3a897109bf93b5c52cc4bcf949d5434e51496459.jsonl"

func publicActionsFixture(t *testing.T) (string, string, string, actionsRequest, *x509.Certificate) {
	t.Helper()
	root := filepath.Join("testdata", "github-actions-public")
	bundlePath := filepath.Join(root, publicActionsBundleName)
	trustedRootPath := filepath.Join(root, "sigstore-public-good-trusted-root.json")
	requestPath := filepath.Join(root, "request.json")
	var request actionsRequest
	if err := decodeClosedJSON(requestPath, &request); err != nil {
		t.Fatal(err)
	}
	entity, err := bundle.LoadJSONFromPath(bundlePath)
	if err != nil {
		t.Fatal(err)
	}
	content, err := entity.VerificationContent()
	if err != nil || content.Certificate() == nil {
		t.Fatal("public Actions fixture certificate unavailable")
	}
	return bundlePath, trustedRootPath, requestPath, request, content.Certificate()
}

func writeActionsRequest(t *testing.T, request actionsRequest) string {
	t.Helper()
	value, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "request.json")
	if err := os.WriteFile(path, value, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func tufUpdateFixture(t *testing.T) ([]byte, tufTrackPackage, []byte) {
	t.Helper()
	publicKey1, privateKey1, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	publicKey2, privateKey2, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	signer1, err := signature.LoadSignerVerifier(privateKey1, crypto.Hash(0))
	if err != nil {
		t.Fatal(err)
	}
	signer2, err := signature.LoadSignerVerifier(privateKey2, crypto.Hash(0))
	if err != nil {
		t.Fatal(err)
	}
	key1, err := metadata.KeyFromPublicKey(publicKey1)
	if err != nil {
		t.Fatal(err)
	}
	key2, err := metadata.KeyFromPublicKey(publicKey2)
	if err != nil {
		t.Fatal(err)
	}
	expires := time.Now().UTC().Add(24 * time.Hour)
	buildRoot := func(version int64, key *metadata.Key, signers ...signature.SignerVerifier) []byte {
		root := metadata.Root(expires)
		root.Signed.Version = version
		for _, role := range []string{
			metadata.ROOT,
			metadata.SNAPSHOT,
			metadata.TARGETS,
			metadata.TIMESTAMP,
		} {
			if err := root.Signed.AddKey(key, role); err != nil {
				t.Fatal(err)
			}
		}
		for _, signer := range signers {
			if _, err := root.Sign(signer); err != nil {
				t.Fatal(err)
			}
		}
		value, err := root.ToBytes(true)
		if err != nil {
			t.Fatal(err)
		}
		return value
	}
	root1 := buildRoot(1, key1, signer1)
	root2 := buildRoot(2, key2, signer1, signer2)
	trustedRoot := []byte("{\"mediaType\":\"application/vnd.dev.sigstore.trustedroot+json;version=0.1\"}\n")

	targets := metadata.Targets(expires)
	targets.Signed.Version = 2
	target, err := metadata.TargetFile().FromBytes("trusted_root.json", trustedRoot)
	if err != nil {
		t.Fatal(err)
	}
	targets.Signed.Targets["trusted_root.json"] = target
	if _, err := targets.Sign(signer2); err != nil {
		t.Fatal(err)
	}
	targetsBytes, err := targets.ToBytes(true)
	if err != nil {
		t.Fatal(err)
	}

	snapshot := metadata.Snapshot(expires)
	snapshot.Signed.Version = 2
	snapshot.Signed.Meta["targets.json"] = metadata.MetaFile(2)
	if _, err := snapshot.Sign(signer2); err != nil {
		t.Fatal(err)
	}
	snapshotBytes, err := snapshot.ToBytes(true)
	if err != nil {
		t.Fatal(err)
	}

	timestamp := metadata.Timestamp(expires)
	timestamp.Signed.Version = 2
	timestamp.Signed.Meta["snapshot.json"] = metadata.MetaFile(2)
	if _, err := timestamp.Sign(signer2); err != nil {
		t.Fatal(err)
	}
	timestampBytes, err := timestamp.ToBytes(true)
	if err != nil {
		t.Fatal(err)
	}
	return root1, tufTrackPackage{
		RootChain:   [][]byte{root2},
		Timestamp:   timestampBytes,
		Snapshot:    snapshotBytes,
		Targets:     targetsBytes,
		TrustedRoot: trustedRoot,
	}, trustedRoot
}

func TestVerifyTUFTrackEnforcesThresholdChainAndTargetIdentity(t *testing.T) {
	root1, update, _ := tufUpdateFixture(t)
	rootPath := filepath.Join(t.TempDir(), "root.json")
	if err := os.WriteFile(rootPath, root1, 0o600); err != nil {
		t.Fatal(err)
	}
	previousTrustedRoot := []byte("{\"previous\":true}\n")
	current := tufCurrentState{
		TUFRootSHA256:     buildIdentity(root1),
		TUFRootVersion:    1,
		TimestampVersion:  1,
		SnapshotVersion:   1,
		TargetsVersion:    1,
		TrustedRootSHA256: buildIdentity(previousTrustedRoot),
	}
	claim, err := verifyTUFTrack(update, rootPath, current, false)
	if err != nil {
		t.Fatalf("expected verified successor, got %v", err)
	}
	if claim.TUFRootVersion != 2 || claim.TimestampVersion != 2 ||
		claim.SnapshotVersion != 2 || claim.TargetsVersion != 2 {
		t.Fatalf("unexpected successor versions: %#v", claim)
	}
	if claim.TrustedRootSHA256 != buildIdentity(update.TrustedRoot) ||
		len(claim.SupersededMaterialIdentities) != 2 ||
		len(claim.RevokedSignerKeyIDs) != 1 {
		t.Fatalf("unexpected material claim: %#v", claim)
	}

	tampered := update
	tampered.TrustedRoot = append([]byte(nil), update.TrustedRoot...)
	tampered.TrustedRoot[0] ^= 1
	if _, err := verifyTUFTrack(tampered, rootPath, current, false); err == nil {
		t.Fatal("tampered trusted root target must be rejected")
	}

	rollbackCurrent := current
	rollbackCurrent.TimestampVersion = 2
	if _, err := verifyTUFTrack(update, rootPath, rollbackCurrent, false); err == nil {
		t.Fatal("metadata rollback or replay must be rejected")
	}
}

func TestVerifyTUFInitialTrackStartsOnlyFromPinnedBootstrapRoot(t *testing.T) {
	root1, update, _ := tufUpdateFixture(t)
	rootPath := filepath.Join(t.TempDir(), "root.json")
	if err := os.WriteFile(rootPath, root1, 0o600); err != nil {
		t.Fatal(err)
	}
	current := tufCurrentState{
		TUFRootSHA256:     buildIdentity(root1),
		TUFRootVersion:    1,
		TimestampVersion:  0,
		SnapshotVersion:   0,
		TargetsVersion:    0,
		TrustedRootSHA256: buildIdentity(root1),
	}

	claim, err := verifyTUFTrack(update, rootPath, current, true)
	if err != nil {
		t.Fatalf("expected verified initial metadata, got %v", err)
	}
	if claim.TUFRootVersion != 2 || len(claim.SupersededMaterialIdentities) != 0 ||
		len(claim.RevokedSignerKeyIDs) != 0 {
		t.Fatalf("unexpected bootstrap claim: %#v", claim)
	}
	current.TrustedRootSHA256 = buildIdentity([]byte("bundle supplied root"))
	if _, err := verifyTUFTrack(update, rootPath, current, true); err == nil {
		t.Fatal("bootstrap request not bound to the pinned root must be rejected")
	}
}

func TestCloseReleaseStatementRequiresExactAuthorityAndSubjects(t *testing.T) {
	request := releaseRequest{
		SchemaVersion: 1,
		Mode:          "github-release",
		Repository:    repository,
		RepositoryID:  repositoryID,
		OwnerID:       ownerID,
		Tag:           "v1.1.0-rc.1",
		TagCommit:     "1111111111111111111111111111111111111111",
		TagObject:     "2222222222222222222222222222222222222222",
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
			{Name: "pkg:github/yanyuhanyue/AniMemo@v1.1.0-rc.1", Digest: map[string]string{"sha1": request.TagObject}},
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
	if claim.TagCommit != request.TagCommit || claim.TagObject != request.TagObject {
		t.Fatalf("annotated tag identities were conflated: %#v", claim)
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

	substitutedTagObject := statement
	substitutedTagObject.Subjects = append([]statementSubject(nil), statement.Subjects...)
	substitutedTagObject.Subjects[0].Digest = map[string]string{"sha1": request.TagCommit}
	if _, err := closeReleaseStatement(request, substitutedTagObject, time.Now()); err == nil {
		t.Fatal("peeled commit must not substitute for the annotated tag object")
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

func TestStableActionsClaimObservesExecutionCommitWithoutAddingItToLogicalRequest(t *testing.T) {
	request := actionsRequest{
		SchemaVersion: 1,
		Mode:          "actions-provenance",
		EvidenceName:  "release-manifest",
		Subject:       expectedSubject{Name: "release-manifest.json", SHA256: digest('a'), Size: 7},
		Workflow:      ".github/workflows/promote-release.yml",
	}
	statement := actionsStatement{
		Type:          "https://in-toto.io/Statement/v1",
		PredicateType: actionsPredicateType,
		Subjects: []statementSubject{{
			Name:   request.Subject.Name,
			Digest: map[string]string{"sha256": trimDigest(request.Subject.SHA256)},
		}},
	}
	observed := "2222222222222222222222222222222222222222"
	claim, err := closeActionsStatement(request, statement, observed)
	if err != nil {
		t.Fatalf("expected observed stable execution commit, got %v", err)
	}
	if claim.Source.Commit != observed || claim.SignerDigest != observed {
		t.Fatalf("execution claim did not retain observed commit: %#v", claim)
	}
	if actionsCertificateExtensions(request).BuildSignerDigest != "" {
		t.Fatal("stable logical request must not predeclare an execution commit")
	}
	request.Workflow = ".github/workflows/release.yml"
	if _, err := closeActionsStatement(request, statement, observed); err == nil {
		t.Fatal("release producer evidence must retain its exact source commit")
	}
}

func TestActionsCertificatePolicySpecifiesIssuerOnlyInMatcher(t *testing.T) {
	request := actionsRequest{
		Workflow:     ".github/workflows/release.yml",
		SourceCommit: "1111111111111111111111111111111111111111",
	}
	identity := "https://github.com/" + repository + "/" + request.Workflow + "@" + actionsSourceRef
	sanMatcher, err := verify.NewSANMatcher(identity, "")
	if err != nil {
		t.Fatal(err)
	}
	issuerMatcher, err := verify.NewIssuerMatcher(actionsOIDCIssuer, "")
	if err != nil {
		t.Fatal(err)
	}
	extensions := actionsCertificateExtensions(request)
	if extensions.Issuer != "" {
		t.Fatal("issuer must be enforced only by IssuerMatcher")
	}
	if _, err := verify.NewCertificateIdentity(sanMatcher, issuerMatcher, extensions); err != nil {
		t.Fatalf("actions certificate policy must be constructible: %v", err)
	}
}

func TestPublicGitHubActionsFixtureInvokesProductionVerifier(t *testing.T) {
	bundlePath, trustedRootPath, requestPath, request, _ := publicActionsFixture(t)
	for path, expected := range map[string]string{
		bundlePath:      "sha256:6f174db7894200a118bc971d86462a0098bdd7766c49f695d1066a8e29d28922",
		trustedRootPath: "sha256:6494e21ea73fa7ee769f85f57d5a3e6a08725eae1e38c755fc3517c9e6bc0b66",
		requestPath:     "sha256:fd047aee8acfb7490bac2571d9b002f34fd55e626a6baf2f84f0c19e8e2a5cd5",
	} {
		value, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if buildIdentity(value) != expected {
			t.Fatalf("committed fixture identity changed: %s", path)
		}
	}
	claim, err := verifyActions(bundlePath, trustedRootPath, requestPath)
	if err != nil {
		t.Fatalf("committed public Actions fixture must verify: %v", err)
	}
	if claim.Subject.Name != request.Subject.Name ||
		claim.Subject.SHA256 != request.Subject.SHA256 ||
		claim.Source.Commit != request.SourceCommit ||
		claim.Workflow != request.Workflow ||
		claim.Repository.RepositoryID != repositoryID ||
		claim.Repository.OwnerID != ownerID {
		t.Fatalf("unexpected production claim: %#v", claim)
	}
}

func TestPublicGitHubActionsFixtureNegativeMatrix(t *testing.T) {
	bundlePath, trustedRootPath, _, request, cert := publicActionsFixture(t)
	identity, err := actionsCertificateIdentity(request)
	if err != nil {
		t.Fatal(err)
	}
	summary, err := summarizeUniqueActionsCertificate(cert)
	if err != nil {
		t.Fatal(err)
	}
	mutations := []struct {
		name   string
		mutate func(*certificate.Summary)
	}{
		{"wrong issuer", func(value *certificate.Summary) {
			value.Extensions.Issuer = "https://issuer.example.invalid"
		}},
		{"wrong repository", func(value *certificate.Summary) {
			value.Extensions.SourceRepositoryURI = "https://github.com/attacker/AniMemo"
		}},
		{"wrong workflow", func(value *certificate.Summary) {
			value.SubjectAlternativeName = "https://github.com/yanyuhanyue/AniMemo/.github/workflows/other.yml@refs/heads/main"
		}},
		{"wrong ref", func(value *certificate.Summary) {
			value.Extensions.SourceRepositoryRef = "refs/heads/release-lookalike"
		}},
		{"wrong SHA", func(value *certificate.Summary) {
			value.Extensions.BuildSignerDigest = "2e699c456110266398f868e43fa2e69b2d704d24"
		}},
	}
	for _, item := range mutations {
		t.Run(item.name, func(t *testing.T) {
			changed := summary
			item.mutate(&changed)
			if err := identity.Verify(changed); err == nil {
				t.Fatal("production certificate identity accepted substituted metadata")
			}
		})
	}

	duplicate := *cert
	duplicate.Extensions = append([]pkix.Extension(nil), cert.Extensions...)
	for _, extension := range cert.Extensions {
		if extension.Id.Equal(certificate.OIDIssuerV2) {
			duplicate.Extensions = append(duplicate.Extensions, extension)
			break
		}
	}
	if _, err := summarizeUniqueActionsCertificate(&duplicate); err == nil {
		t.Fatal("duplicate issuer extension must be rejected")
	}

	for _, requiredOID := range []struct {
		name  string
		equal func(pkix.Extension) bool
	}{
		{"runner environment", func(value pkix.Extension) bool { return value.Id.Equal(certificate.OIDRunnerEnvironment) }},
		{"repository URI", func(value pkix.Extension) bool { return value.Id.Equal(certificate.OIDSourceRepositoryURI) }},
		{"repository owner URI", func(value pkix.Extension) bool { return value.Id.Equal(certificate.OIDSourceRepositoryOwnerURI) }},
		{"signer digest", func(value pkix.Extension) bool { return value.Id.Equal(certificate.OIDBuildSignerDigest) }},
		{"repository digest", func(value pkix.Extension) bool { return value.Id.Equal(certificate.OIDSourceRepositoryDigest) }},
		{"repository ref", func(value pkix.Extension) bool { return value.Id.Equal(certificate.OIDSourceRepositoryRef) }},
	} {
		t.Run("missing "+requiredOID.name, func(t *testing.T) {
			missing := *cert
			missing.Extensions = nil
			for _, extension := range cert.Extensions {
				if !requiredOID.equal(extension) {
					missing.Extensions = append(missing.Extensions, extension)
				}
			}
			if _, err := summarizeUniqueActionsCertificate(&missing); err == nil {
				t.Fatal("missing required extension must be rejected")
			}
		})
	}

	wrongWorkflow := request
	wrongWorkflow.Workflow = ".github/workflows/promote-release.yml"
	if _, err := verifyActions(
		bundlePath, trustedRootPath, writeActionsRequest(t, wrongWorkflow),
	); err == nil {
		t.Fatal("production verifier accepted wrong workflow")
	}
	wrongSHA := request
	wrongSHA.SourceCommit = "2e699c456110266398f868e43fa2e69b2d704d24"
	if _, err := verifyActions(
		bundlePath, trustedRootPath, writeActionsRequest(t, wrongSHA),
	); err == nil {
		t.Fatal("production verifier accepted wrong source SHA")
	}
	wrongSubject := request
	wrongSubject.Subject.SHA256 = digest('b')
	if _, err := verifyActions(
		bundlePath, trustedRootPath, writeActionsRequest(t, wrongSubject),
	); err == nil {
		t.Fatal("production verifier accepted wrong subject")
	}

	malformed := filepath.Join(t.TempDir(), "malformed.bundle.json")
	if err := os.WriteFile(malformed, []byte("{\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := verifyActions(
		malformed, trustedRootPath, writeActionsRequest(t, request),
	); err == nil {
		t.Fatal("production verifier accepted malformed bundle")
	}
	untrustedRoot := filepath.Join(
		"testdata", "github-actions-public", "untrusted-scaffolding-root.json",
	)
	untrustedBytes, err := os.ReadFile(untrustedRoot)
	if err != nil {
		t.Fatal(err)
	}
	if buildIdentity(untrustedBytes) != "sha256:503c669ede6b4416c39de5d48d5964141970d9881fc941370faac0f75789fecf" {
		t.Fatal("untrusted-chain fixture identity changed")
	}
	if _, err := verifyActions(
		bundlePath, untrustedRoot, writeActionsRequest(t, request),
	); err == nil {
		t.Fatal("production verifier accepted untrusted certificate chain")
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
