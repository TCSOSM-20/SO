pipeline {
	agent any
	stages {
		stage("Build") {
			agent {
				dockerfile true
			}
			steps {
				sh 'make NOT_DEVELOPER_BUILD=TRUE -j16 package'
				stash name: "deb-files", includes: ".build/*.deb"
			}
		}
		stage("Repo Component") {
			steps {
				unstash "deb-files"
				sh '''
					mkdir -p pool/SO
					mv .build/*.deb pool/SO/
					mkdir -p dists/$RELEASE/SO/binary-amd64/
					apt-ftparchive packages pool/SO > dists/$RELEASE/SO/binary-amd64/Packages
					gzip -9fk dists/$RELEASE/SO/binary-amd64/Packages
					'''
				archiveArtifacts artifacts: "dists/**,pool/SO/*.deb"
			}
		}
	}
}
