// Command release-attestation-verifier performs network-free verification of
// GitHub Immutable Release attestations. Cryptography and Sigstore bundle
// validation are delegated to the pinned sigstore-go implementation.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"path"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/sigstore/sigstore-go/pkg/bundle"
	"github.com/sigstore/sigstore-go/pkg/fulcio/certificate"
	"github.com/sigstore/sigstore-go/pkg/root"
	"github.com/sigstore/sigstore-go/pkg/verify"
	"google.golang.org/protobuf/encoding/protojson"
)

const (
	repository                  = "yanyuhanyue/AniMemo"
	repositoryID                = "1327429673"
	ownerID                     = "111261350"
	releasePredicateType        = "https://in-toto.io/attestation/release/v0.2"
	releaseCertificateIdentity  = "https://dotcom.releases.github.com"
	releaseCertificateIssuerOrg = "GitHub, Inc."
	actionsPredicateType        = "https://slsa.dev/provenance/v1"
	actionsOIDCIssuer           = "https://token.actions.githubusercontent.com"
	actionsSourceRef            = "refs/heads/main"
	verifierVersion             = "2.97.0+animemo.1"
)

var (
	sha256Identity = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	commitIdentity = regexp.MustCompile(`^[0-9a-f]{40}$`)
	tagIdentity    = regexp.MustCompile(
		`^v[0-9]+\.[0-9]+\.[0-9]+(?:-(?:beta|rc)\.[1-9][0-9]*)?$`,
	)
)

type expectedSubject struct {
	Name   string `json:"name"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

type releaseRequest struct {
	SchemaVersion    int               `json:"schemaVersion"`
	Mode             string            `json:"mode"`
	Repository       string            `json:"repository"`
	RepositoryID     string            `json:"repositoryId"`
	OwnerID          string            `json:"ownerId"`
	Tag              string            `json:"tag"`
	TagCommit        string            `json:"tagCommit"`
	ExpectedSubjects []expectedSubject `json:"expectedSubjects"`
}

type actionsRequest struct {
	SchemaVersion int             `json:"schemaVersion"`
	Mode          string          `json:"mode"`
	EvidenceName  string          `json:"evidenceName"`
	Subject       expectedSubject `json:"subject"`
	Workflow      string          `json:"workflow"`
	SourceCommit  string          `json:"sourceCommit"`
}

type statementSubject struct {
	Name   string            `json:"name,omitempty"`
	URI    string            `json:"uri,omitempty"`
	Digest map[string]string `json:"digest"`
}

func (s statementSubject) identity() string {
	if s.Name != "" {
		return s.Name
	}
	return s.URI
}

type releasePredicate struct {
	DatabaseID   string `json:"databaseId"`
	OwnerID      string `json:"ownerId"`
	PackageID    string `json:"packageId"`
	PURL         string `json:"purl"`
	Repository   string `json:"repository"`
	RepositoryID string `json:"repositoryId"`
	Tag          string `json:"tag"`
}

type releaseStatement struct {
	Type          string             `json:"_type"`
	Subjects      []statementSubject `json:"subject"`
	PredicateType string             `json:"predicateType"`
	Predicate     releasePredicate   `json:"predicate"`
}

type actionsStatement struct {
	Type          string             `json:"_type"`
	Subjects      []statementSubject `json:"subject"`
	PredicateType string             `json:"predicateType"`
}

type releaseAsset struct {
	Name   string `json:"name"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

type releaseTransportAsset struct {
	Name          string `json:"name"`
	SHA256        string `json:"sha256"`
	Size          int64  `json:"size"`
	Role          string `json:"role"`
	AuthorityRole string `json:"authorityRole"`
}

type releaseClaim struct {
	SchemaVersion int    `json:"schemaVersion"`
	PredicateType string `json:"predicateType"`
	Immutable     bool   `json:"immutable"`
	Repository    struct {
		Name         string `json:"name"`
		RepositoryID string `json:"repositoryId"`
		OwnerID      string `json:"ownerId"`
	} `json:"repository"`
	Tag         string `json:"tag"`
	TagCommit   string `json:"tagCommit"`
	Draft       bool   `json:"draft"`
	Prerelease  bool   `json:"prerelease"`
	SignedAt    string `json:"signedAt"`
	Certificate struct {
		Identity           string `json:"identity"`
		IssuerOrganization string `json:"issuerOrganization"`
	} `json:"certificate"`
	Assets          []releaseAsset          `json:"assets"`
	TransportAssets []releaseTransportAsset `json:"transportAssets"`
}

type actionsClaim struct {
	SchemaVersion int    `json:"schemaVersion"`
	PredicateType string `json:"predicateType"`
	Subject       struct {
		Name   string `json:"name"`
		SHA256 string `json:"sha256"`
	} `json:"subject"`
	Repository struct {
		Name         string `json:"name"`
		RepositoryID string `json:"repositoryId"`
		OwnerID      string `json:"ownerId"`
	} `json:"repository"`
	Workflow    string `json:"workflow"`
	Certificate struct {
		Identity string `json:"identity"`
		Issuer   string `json:"issuer"`
	} `json:"certificate"`
	Source struct {
		Commit string `json:"commit"`
		Ref    string `json:"ref"`
	} `json:"source"`
	SignerDigest string `json:"signerDigest"`
}

func trimDigest(value string) string { return strings.TrimPrefix(value, "sha256:") }

func closeReleaseStatement(request releaseRequest, statement releaseStatement, signedAt time.Time) (releaseClaim, error) {
	var claim releaseClaim
	if request.SchemaVersion != 1 || request.Mode != "github-release" ||
		request.Repository != repository || request.RepositoryID != repositoryID ||
		request.OwnerID != ownerID || !tagIdentity.MatchString(request.Tag) ||
		!commitIdentity.MatchString(request.TagCommit) {
		return claim, errors.New("发布验证请求身份未关闭")
	}
	if statement.Type != "https://in-toto.io/Statement/v1" ||
		statement.PredicateType != releasePredicateType ||
		statement.Predicate.Repository != repository ||
		statement.Predicate.RepositoryID != repositoryID ||
		statement.Predicate.PackageID != repositoryID ||
		statement.Predicate.OwnerID != ownerID ||
		statement.Predicate.Tag != request.Tag ||
		statement.Predicate.PURL != "pkg:github/yanyuhanyue/AniMemo@"+request.Tag {
		return claim, errors.New("发布声明仓库、标签或数字身份不一致")
	}
	expectedNames := map[string]expectedSubject{}
	for _, subject := range request.ExpectedSubjects {
		if subject.Name == "" || !sha256Identity.MatchString(subject.SHA256) || subject.Size < 0 {
			return claim, errors.New("发布验证请求 subject 无效")
		}
		if _, exists := expectedNames[subject.Name]; exists {
			return claim, errors.New("发布验证请求 subject 重复")
		}
		expectedNames[subject.Name] = subject
	}
	portableName := "animemo-" + request.Tag + "-portable.tar"
	required := map[string]bool{
		"checksums.txt":            false,
		"deployment-contract.json": false,
		"installer-materials.tar":  false,
		"release-manifest.json":    false,
		portableName:               false,
	}
	if len(expectedNames) != len(required) {
		return claim, errors.New("发布验证请求 subject 集合未关闭")
	}
	for name := range expectedNames {
		if _, ok := required[name]; !ok {
			return claim, errors.New("发布验证请求含额外 subject")
		}
	}
	if len(statement.Subjects) != len(required)+1 {
		return claim, errors.New("签名发布声明 subject 数量不一致")
	}
	tagSeen := false
	for _, subject := range statement.Subjects {
		name := subject.identity()
		if name == "pkg:github/yanyuhanyue/AniMemo@"+request.Tag {
			if tagSeen || len(subject.Digest) != 1 || subject.Digest["sha1"] != request.TagCommit {
				return claim, errors.New("签名发布声明 tag commit 绑定无效")
			}
			tagSeen = true
			continue
		}
		expected, ok := expectedNames[name]
		if !ok || required[name] || len(subject.Digest) != 1 ||
			subject.Digest["sha256"] != trimDigest(expected.SHA256) {
			return claim, errors.New("签名发布声明资产集合或 digest 不一致")
		}
		required[name] = true
	}
	if !tagSeen {
		return claim, errors.New("签名发布声明缺少 tag commit")
	}
	for _, seen := range required {
		if !seen {
			return claim, errors.New("签名发布声明资产集合不完整")
		}
	}
	if signedAt.IsZero() {
		return claim, errors.New("签名发布声明缺少已验证时间戳")
	}
	claim.SchemaVersion = 1
	claim.PredicateType = releasePredicateType
	claim.Immutable = true
	claim.Repository.Name = repository
	claim.Repository.RepositoryID = repositoryID
	claim.Repository.OwnerID = ownerID
	claim.Tag = request.Tag
	claim.TagCommit = request.TagCommit
	claim.Draft = false
	claim.Prerelease = strings.Contains(request.Tag, "-")
	claim.SignedAt = signedAt.UTC().Format(time.RFC3339)
	claim.Certificate.Identity = releaseCertificateIdentity
	claim.Certificate.IssuerOrganization = releaseCertificateIssuerOrg
	for _, name := range []string{"checksums.txt", "deployment-contract.json", "installer-materials.tar", "release-manifest.json"} {
		subject := expectedNames[name]
		claim.Assets = append(claim.Assets, releaseAsset(subject))
	}
	portable := expectedNames[portableName]
	claim.TransportAssets = []releaseTransportAsset{{
		Name:          portable.Name,
		SHA256:        portable.SHA256,
		Size:          portable.Size,
		Role:          "PORTABLE_RELEASE_BUNDLE",
		AuthorityRole: "TRANSPORT_ONLY",
	}}
	return claim, nil
}

func closeActionsStatement(request actionsRequest, statement actionsStatement) (actionsClaim, error) {
	var claim actionsClaim
	workflows := map[string]bool{
		".github/workflows/release.yml":         true,
		".github/workflows/promote-release.yml": true,
	}
	evidenceNames := map[string]bool{
		"api-image": true, "web-image": true, "release-manifest": true,
		"deployment-contract": true, "installer-materials": true,
	}
	if request.SchemaVersion != 1 || request.Mode != "actions-provenance" ||
		!evidenceNames[request.EvidenceName] || !workflows[request.Workflow] ||
		!commitIdentity.MatchString(request.SourceCommit) ||
		request.Subject.Name == "" || !sha256Identity.MatchString(request.Subject.SHA256) {
		return claim, errors.New("Actions 验证请求身份未关闭")
	}
	allowedSubject := map[string]string{
		"api-image":           "ghcr.io/yanyuhanyue/animemo-api",
		"web-image":           "ghcr.io/yanyuhanyue/animemo-web",
		"release-manifest":    "release-manifest.json",
		"deployment-contract": "deployment-contract.json",
		"installer-materials": "installer-materials.tar",
	}
	if request.Subject.Name != allowedSubject[request.EvidenceName] ||
		statement.Type != "https://in-toto.io/Statement/v1" ||
		statement.PredicateType != actionsPredicateType {
		return claim, errors.New("Actions subject 或 predicate 身份不一致")
	}
	found := false
	for _, subject := range statement.Subjects {
		if len(subject.Digest) != 1 || subject.Digest["sha256"] != trimDigest(request.Subject.SHA256) {
			continue
		}
		name := subject.identity()
		if request.EvidenceName == "api-image" || request.EvidenceName == "web-image" {
			expected := request.Subject.Name
			if !matchesOCISubject(name, expected, request.Subject.SHA256) {
				continue
			}
		} else if name != request.Subject.Name {
			continue
		}
		if found {
			return claim, errors.New("Actions subject 重复")
		}
		found = true
	}
	if !found {
		return claim, errors.New("Actions 签名声明未绑定预期 subject")
	}
	claim.SchemaVersion = 1
	claim.PredicateType = actionsPredicateType
	claim.Subject.Name = request.Subject.Name
	claim.Subject.SHA256 = request.Subject.SHA256
	claim.Repository.Name = repository
	claim.Repository.RepositoryID = repositoryID
	claim.Repository.OwnerID = ownerID
	claim.Workflow = request.Workflow
	claim.Certificate.Identity = "https://github.com/" + repository + "/" + request.Workflow + "@" + actionsSourceRef
	claim.Certificate.Issuer = actionsOIDCIssuer
	claim.Source.Commit = request.SourceCommit
	claim.Source.Ref = actionsSourceRef
	claim.SignerDigest = request.SourceCommit
	return claim, nil
}

func matchesOCISubject(name, repositoryName, digest string) bool {
	if name == repositoryName {
		return true
	}
	prefix := "pkg:oci/" + path.Base(repositoryName) + "@" + digest
	if !strings.HasPrefix(name, prefix+"?") {
		return false
	}
	parsed, err := url.Parse(name)
	if err != nil {
		return false
	}
	repositoryURL := parsed.Query().Get("repository_url")
	return repositoryURL == repositoryName || repositoryURL == "https://"+repositoryName
}

func decodeClosedJSON(path string, target any) error {
	value, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if err := rejectDuplicateJSONKeys(value); err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(value))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		return errors.New("JSON 含额外值或尾随数据")
	}
	return nil
}

func rejectDuplicateJSONKeys(value []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(value))
	var walk func() error
	walk = func() error {
		token, err := decoder.Token()
		if err != nil {
			return err
		}
		delimiter, ok := token.(json.Delim)
		if !ok {
			return nil
		}
		switch delimiter {
		case '{':
			seen := map[string]bool{}
			for decoder.More() {
				keyToken, err := decoder.Token()
				if err != nil {
					return err
				}
				key, ok := keyToken.(string)
				if !ok {
					return errors.New("JSON object key 无效")
				}
				if seen[key] {
					return errors.New("JSON 含重复 key")
				}
				seen[key] = true
				if err := walk(); err != nil {
					return err
				}
			}
			closing, err := decoder.Token()
			if err != nil || closing != json.Delim('}') {
				return errors.New("JSON object 未关闭")
			}
		case '[':
			for decoder.More() {
				if err := walk(); err != nil {
					return err
				}
			}
			closing, err := decoder.Token()
			if err != nil || closing != json.Delim(']') {
				return errors.New("JSON array 未关闭")
			}
		default:
			return errors.New("JSON delimiter 无效")
		}
		return nil
	}
	if err := walk(); err != nil {
		return err
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return errors.New("JSON 含第二个顶层值")
	}
	return nil
}

func verifyRelease(bundlePath, trustedRootPath, requestPath string) (releaseClaim, error) {
	var request releaseRequest
	if err := decodeClosedJSON(requestPath, &request); err != nil {
		return releaseClaim{}, fmt.Errorf("发布验证请求不可解析: %w", err)
	}
	rootJSON, err := os.ReadFile(trustedRootPath)
	if err != nil {
		return releaseClaim{}, fmt.Errorf("预置 GitHub 信任根不可读: %w", err)
	}
	trustedRoot, err := root.NewTrustedRootFromJSON(rootJSON)
	if err != nil {
		return releaseClaim{}, fmt.Errorf("预置 GitHub 信任根无效: %w", err)
	}
	entity, err := bundle.LoadJSONFromPath(bundlePath)
	if err != nil {
		return releaseClaim{}, fmt.Errorf("Sigstore bundle 无效: %w", err)
	}
	verifier, err := verify.NewVerifier(trustedRoot, verify.WithSignedTimestamps(1))
	if err != nil {
		return releaseClaim{}, fmt.Errorf("GitHub verifier 初始化失败: %w", err)
	}
	sanMatcher, err := verify.NewSANMatcher(releaseCertificateIdentity, "")
	if err != nil {
		return releaseClaim{}, err
	}
	issuerMatcher, err := verify.NewIssuerMatcher("", ".*")
	if err != nil {
		return releaseClaim{}, err
	}
	certIdentity, err := verify.NewCertificateIdentity(sanMatcher, issuerMatcher, certificate.Extensions{})
	if err != nil {
		return releaseClaim{}, err
	}
	commit, err := hex.DecodeString(request.TagCommit)
	if err != nil {
		return releaseClaim{}, errors.New("tag commit 无效")
	}
	result, err := verifier.Verify(entity, verify.NewPolicy(
		verify.WithArtifactDigest("sha1", commit),
		verify.WithCertificateIdentity(certIdentity),
	))
	if err != nil {
		return releaseClaim{}, fmt.Errorf("GitHub Immutable Release 密码学验证失败: %w", err)
	}
	verificationContent, err := entity.VerificationContent()
	if err != nil || verificationContent.Certificate() == nil {
		return releaseClaim{}, errors.New("GitHub Release 已验证证书不可读取")
	}
	issuerOrganizations := verificationContent.Certificate().Issuer.Organization
	if len(issuerOrganizations) != 1 || issuerOrganizations[0] != releaseCertificateIssuerOrg {
		return releaseClaim{}, errors.New("GitHub Release 证书 issuer organization 不一致")
	}
	statementJSON, err := protojson.Marshal(result.Statement)
	if err != nil {
		return releaseClaim{}, fmt.Errorf("已验证发布声明不可编码: %w", err)
	}
	var statement releaseStatement
	if err := json.Unmarshal(statementJSON, &statement); err != nil {
		return releaseClaim{}, fmt.Errorf("已验证发布声明不可解析: %w", err)
	}
	if len(result.VerifiedTimestamps) != 1 {
		return releaseClaim{}, errors.New("GitHub 发布证明时间戳数量不为一")
	}
	return closeReleaseStatement(request, statement, result.VerifiedTimestamps[0].Timestamp)
}

func verifyActions(bundlePath, trustedRootPath, requestPath string) (actionsClaim, error) {
	var request actionsRequest
	if err := decodeClosedJSON(requestPath, &request); err != nil {
		return actionsClaim{}, fmt.Errorf("Actions 验证请求不可解析: %w", err)
	}
	rootJSON, err := os.ReadFile(trustedRootPath)
	if err != nil {
		return actionsClaim{}, fmt.Errorf("预置 Sigstore 信任根不可读: %w", err)
	}
	trustedRoot, err := root.NewTrustedRootFromJSON(rootJSON)
	if err != nil {
		return actionsClaim{}, fmt.Errorf("预置 Sigstore 信任根无效: %w", err)
	}
	entity, err := bundle.LoadJSONFromPath(bundlePath)
	if err != nil {
		return actionsClaim{}, fmt.Errorf("Sigstore bundle 无效: %w", err)
	}
	verifier, err := verify.NewVerifier(
		trustedRoot,
		verify.WithSignedCertificateTimestamps(1),
		verify.WithTransparencyLog(1),
		verify.WithObserverTimestamps(1),
	)
	if err != nil {
		return actionsClaim{}, fmt.Errorf("Sigstore verifier 初始化失败: %w", err)
	}
	identity := "https://github.com/" + repository + "/" + request.Workflow + "@" + actionsSourceRef
	sanMatcher, err := verify.NewSANMatcher(identity, "")
	if err != nil {
		return actionsClaim{}, err
	}
	issuerMatcher, err := verify.NewIssuerMatcher(actionsOIDCIssuer, "")
	if err != nil {
		return actionsClaim{}, err
	}
	certIdentity, err := verify.NewCertificateIdentity(sanMatcher, issuerMatcher, certificate.Extensions{
		Issuer:                   actionsOIDCIssuer,
		RunnerEnvironment:        "github-hosted",
		SourceRepositoryURI:      "https://github.com/" + repository,
		SourceRepositoryOwnerURI: "https://github.com/yanyuhanyue",
		BuildSignerDigest:        request.SourceCommit,
		SourceRepositoryDigest:   request.SourceCommit,
		SourceRepositoryRef:      actionsSourceRef,
	})
	if err != nil {
		return actionsClaim{}, err
	}
	digestBytes, err := hex.DecodeString(trimDigest(request.Subject.SHA256))
	if err != nil {
		return actionsClaim{}, errors.New("Actions subject digest 无效")
	}
	result, err := verifier.Verify(entity, verify.NewPolicy(
		verify.WithArtifactDigest("sha256", digestBytes),
		verify.WithCertificateIdentity(certIdentity),
	))
	if err != nil {
		return actionsClaim{}, fmt.Errorf("Actions provenance 密码学验证失败: %w", err)
	}
	statementJSON, err := protojson.Marshal(result.Statement)
	if err != nil {
		return actionsClaim{}, fmt.Errorf("已验证 Actions 声明不可编码: %w", err)
	}
	var statement actionsStatement
	if err := json.Unmarshal(statementJSON, &statement); err != nil {
		return actionsClaim{}, fmt.Errorf("已验证 Actions 声明不可解析: %w", err)
	}
	return closeActionsStatement(request, statement)
}

func canonicalJSON(value any) ([]byte, error) {
	encoded, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var normalized any
	if err := json.Unmarshal(encoded, &normalized); err != nil {
		return nil, err
	}
	// encoding/json deterministically sorts string map keys.
	return json.Marshal(normalized)
}

func main() {
	bundlePath := flag.String("bundle", "", "本地 Sigstore bundle JSON")
	trustedRootPath := flag.String("trusted-root", "", "预置 GitHub trusted_root.json")
	requestPath := flag.String("request", "", "关闭的验证请求 JSON")
	showVersion := flag.Bool("version", false, "输出验证器版本")
	flag.Parse()
	if *showVersion {
		fmt.Println(verifierVersion)
		return
	}
	if *bundlePath == "" || *trustedRootPath == "" || *requestPath == "" || flag.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "验证器参数未关闭")
		os.Exit(2)
	}
	requestBytes, err := os.ReadFile(*requestPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "验证请求不可读")
		os.Exit(1)
	}
	var mode struct {
		Mode string `json:"mode"`
	}
	if err := json.Unmarshal(requestBytes, &mode); err != nil {
		fmt.Fprintln(os.Stderr, "验证请求不可解析")
		os.Exit(1)
	}
	var claim any
	switch mode.Mode {
	case "github-release":
		claim, err = verifyRelease(*bundlePath, *trustedRootPath, *requestPath)
	case "actions-provenance":
		claim, err = verifyActions(*bundlePath, *trustedRootPath, *requestPath)
	default:
		err = errors.New("验证模式未关闭")
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	encoded, err := canonicalJSON(claim)
	if err != nil {
		fmt.Fprintln(os.Stderr, "验证结果不可编码")
		os.Exit(1)
	}
	fmt.Println(string(encoded))
}

// buildIdentity is used by conformance tests and packaging review to bind the
// exact verifier source request without introducing a second crypto scheme.
func buildIdentity(value []byte) string {
	sum := sha256.Sum256(value)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func sortedSubjectNames(subjects []expectedSubject) []string {
	names := make([]string, len(subjects))
	for i := range subjects {
		names[i] = subjects[i].Name
	}
	sort.Strings(names)
	return names
}
