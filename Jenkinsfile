// Lints and tests SecretRotator on Python 3.13, the iac image's, then resets the prd branch to the
// commit it tested and starts IaC/IaC Docker Image with image=iac, which installs prd's tip into
// the iac image srviac runs the rotator from. A red build moves nothing.
//
// prd is the last green commit: every rebuild of the iac image installs it, the version poller's
// and an Ansible change's too, so a red build on main never reaches srviac.
//
// Controller config:
//   - Job: IaC/SecretRotator
//   - SCM: pvginkel/SecretRotator, branch main
//   - Script Path: Jenkinsfile

library identifier: 'JenkinsPipelineUtils', changelog: false

pipeline {
    agent {
        kubernetes {
            inheritFrom 'jenkins-agent'
            // The gates run in the image `cexec iac` runs them in locally: Python 3.13, the iac
            // image's version, with poetry.
            yaml podYaml(templates: ['iac-toolchain'])
        }
    }

    options {
        // Without abortPrevious: an abort between the prd push and the start of IaC/IaC Docker
        // Image leaves prd ahead of the rotator the iac image carries.
        disableConcurrentBuilds()
        skipDefaultCheckout()
        timeout(time: 60, unit: 'MINUTES')
        timestamps()
    }

    triggers {
        githubPush()
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        // Each gate is its .kubecoder/project.yaml verb without its cexec prefix.
        stage('Lint') {
            steps {
                container('iac-toolchain') {
                    sh 'poetry install --no-interaction'
                    sh 'poetry run ruff check .'
                    sh 'poetry run ruff format --check .'
                }
            }
        }

        stage('Test') {
            steps {
                container('iac-toolchain') {
                    sh 'poetry run pytest -q'
                }
            }
        }

        // The Checkout stage's clone holds no credential to push with, so the push goes from a clone
        // made inside withCredentials. --force: prd is reset to the commit this build tested,
        // whatever main's history did since the last green build.
        stage('Reset prd branch') {
            steps {
                script {
                    String commit = sh(script: 'git rev-parse HEAD', returnStdout: true).trim()
                    withCredentials([usernamePassword(
                        credentialsId: '5f6fbd66-b41c-405f-b107-85ba6fd97f10',
                        usernameVariable: 'GIT_USER',
                        passwordVariable: 'GIT_TOKEN')]) {
                        sh """
                            set -eu
                            git clone --quiet "https://\$GIT_USER:\$GIT_TOKEN@github.com/pvginkel/SecretRotator.git" prd
                            git -C prd push --quiet --force origin '${commit}:refs/heads/prd'
                        """
                    }
                }
            }
        }

        // image=iac builds the image whatever that build's change sets hold.
        stage('Trigger IaC Docker Image') {
            steps {
                build job: 'IaC Docker Image', parameters: [string(name: 'image', value: 'iac')], wait: false
            }
        }
    }

    post {
        aborted {
            script {
                notify.error("${env.JOB_NAME} #${env.BUILD_NUMBER} aborted (timeout or hand)")
            }
        }
    }
}
